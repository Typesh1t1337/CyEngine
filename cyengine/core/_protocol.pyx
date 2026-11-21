# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: nonecheck=False
"""
CyFrame Protocol — TCP → HTTP → handler → TCP.

Цепочка на каждый запрос:
  data_received → httptools (C parser) → Request → handler → Response.render() → transport.write()

Ключевые решения:
  - HttpProtocol использует __slots__ — нет __dict__, быстрый доступ к атрибутам
  - keep-alive из коробки — _reset() переиспользует соединение
  - transport.write() синхронный — не await, данные уходят сразу в kernel buffer
  - все преаллоцированные ответы (404, 405, 500) создаются один раз при импорте
"""

import asyncio
import logging
import httptools

from cyengine.core._types import (
    Request, Response, date_header,
    RouterNotFound, MethodNotAllowed,
)

from cyengine.errors import HTTPError

log = logging.getLogger("cyengine")


# ─── Преаллоцированные ответы на типовые ошибки ──────────────────────────────
# Создаются один раз при импорте модуля, не на каждый запрос

# Отказы уровня соединения (до/вне цепочки хендлеров): (код, фраза).
# Тело собирается в момент отправки — чтобы в ответе был актуальный Date.

cdef dict _HEADER_NAMES = {}
cdef dict _METHODS = {m: m.decode() for m in
                      (b"GET", b"HEAD", b"POST", b"PUT", b"DELETE", b"PATCH", b"OPTIONS")}

cdef int _DL_IDLE = 1
cdef int _DL_READ = 2

cdef tuple _E400 = (400, "Bad Request")
cdef tuple _E408 = (408, "Request Timeout")
cdef tuple _E413 = (413, "Payload Too Large")
cdef tuple _E431 = (431, "Request Header Fields Too Large")
cdef tuple _E500 = (500, "Internal Server Error")
cdef tuple _E503 = (503, "Service Unavailable")


def _raw(tuple err):
    code, reason = err
    body = reason.encode()
    return (
        f"HTTP/1.1 {code} {reason}\r\nServer: cyframe\r\n".encode()
        + date_header()
        + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        + body
    )


# ─── Protocol ────────────────────────────────────────────────────────────────

class HttpProtocol(asyncio.Protocol):
    """
    asyncio.Protocol — одна инстанция на TCP соединение.

    Почему не cdef класс:
      asyncio требует наследования от Protocol — Python класс.
      Компенсируем через __slots__ и минимум Python объектов на горячем пути.

    Жизненный цикл:
      connection_made → [data_received]* → connection_lost
      один цикл data_received → on_message_complete = один HTTP запрос
    """

    __slots__ = (
        "_transport",   # asyncio.Transport — пишем байты сюда
        "_parser",      # httptools.HttpRequestParser
        "_request",     # текущий Request (сбрасывается на keep-alive)
        "_router",      # Router из App
        "_app",         # App — для доступа к пулам и провайдерам
        "_loop",        # event loop — для create_task / call_later
        "_deadline",    # loop.time(), когда сработает read/keep-alive таймаут (0 — нет)
        "_deadline_kind",  # _DL_IDLE / _DL_READ
        "_inflight",    # запросов в обработке на этом соединении
        "_receiving",   # True, пока текущий запрос принят не полностью
        "_reject",      # готовый ответ-отказ (413/431/...) → закрыть соединение
        "_headers_done",
        "_hdr_bytes",   # размер URL + заголовков (по колбэкам парсера)
        "_recv_bytes",  # байт получено до конца заголовков
        "_max_header", "_max_body",
        "_read_timeout", "_keepalive_timeout", "_handler_timeout",
    )

    def __init__(self, router, app, loop):
        self._router    = router
        self._app       = app
        self._loop      = loop
        self._transport = None
        self._parser    = None
        self._request   = None
        self._deadline  = 0.0
        self._deadline_kind = _DL_IDLE
        self._inflight  = 0
        self._receiving = False
        self._reject    = None
        self._headers_done = False
        self._hdr_bytes  = 0
        self._recv_bytes = 0
        lim = app._limits
        self._max_header        = lim.max_header_size
        self._max_body          = lim.max_body_size
        self._read_timeout      = lim.read_timeout
        self._keepalive_timeout = lim.keepalive_timeout
        self._handler_timeout   = lim.handler_timeout

    # ── asyncio callbacks ────────────────────────────────────────────────────

    def connection_made(self, transport):
        self._transport = transport
        self._app._protocols.add(self)
        self._reset()
        self._arm_idle()

    def connection_lost(self, exc):
        self._transport = None
        self._app._protocols.discard(self)

    def close(self):
        if self._transport is not None:
            self._transport.close()

    def data_received(self, data: bytes):
        if self._reject is not None:
            return

        if not self._receiving:
            # первый байт нового запроса: простой закончился, пошёл read-таймаут
            self._receiving = True
            self._arm(self._read_timeout, _DL_READ)

        was_headers_done = self._headers_done
        try:
            self._parser.feed_data(data)
        except httptools.HttpParserError:
            self._fail(_E400)
            return

        if (self._reject is None and not was_headers_done and not self._headers_done
                and self._max_header is not None):
            # заголовки ещё не закончились — парсер копит байты без колбэков
            self._recv_bytes += len(data)
            if self._recv_bytes > self._max_header:
                self._reject = _E431

        if self._reject is not None:
            self._fail(self._reject)

    # ── httptools callbacks (вызываются синхронно из feed_data) ──────────────

    def on_url(self, url: bytes):
        cdef object req = self._request
        self._hdr_bytes += len(url)
        if self._max_header is not None and self._hdr_bytes > self._max_header:
            self._reject = _E431
            return
        if b"?" in url:
            decoded = url.decode("ascii", errors="replace")
            idx = decoded.index("?")
            req.path         = decoded[:idx]
            req.query_string = url[idx + 1:]   # bytes — парсится лениво
        else:
            req.path = url.decode("ascii", errors="replace")

    def on_header(self, name: bytes, value: bytes):
        self._hdr_bytes += len(name) + len(value) + 4
        if self._max_header is not None and self._hdr_bytes > self._max_header:
            self._reject = _E431
            return
        # имена заголовков повторяются от запроса к запросу — кэш вместо decode+lower
        key = _HEADER_NAMES.get(name)
        if key is None:
            key = name.decode("latin-1").lower()
            if len(_HEADER_NAMES) < 512:      # ограничен: клиент не раздует память
                _HEADER_NAMES[name] = key
        self._request.headers[key] = value.decode("latin-1")

    def on_headers_complete(self):
        self._headers_done = True
        raw = self._parser.get_method()
        method = _METHODS.get(raw)
        self._request.method = method if method is not None else raw.decode("ascii")

        if self._max_body is not None:
            cl = self._request.headers.get("content-length")
            if cl is not None:
                try:
                    too_big = int(cl) > self._max_body
                except ValueError:
                    too_big = False
                if too_big:
                    self._reject = _E413

    def on_body(self, body: bytes):
        if self._reject is not None:
            return
        if (self._max_body is not None
                and len(self._request._body) + len(body) > self._max_body):
            self._reject = _E413
            return
        req = self._request
        cur = req._body
        if not cur:
            req._body = body                 # единственный кусок — без копии
        elif type(cur) is bytes:
            buf = bytearray(cur)
            buf += body
            req._body = buf
        else:
            cur += body

    def on_message_complete(self):
        if self._reject is not None:
            return
        cdef bint keep_alive = self._parser.should_keep_alive()
        self._receiving = False
        self._deadline = 0.0
        self._inflight += 1
        task = self._loop.create_task(self._handle(self._request, keep_alive))
        # дедлайн хендлера лежит в словаре; отменяет его sweeper (server.py)
        self._app._tasks[task] = (
            self._loop.time() + self._handler_timeout
            if self._handler_timeout is not None else 0.0
        )
        task.add_done_callback(self._task_done)
        if keep_alive:
            self._reset()

    # ── обработка запроса ────────────────────────────────────────────────────

    async def _handle(self, request, bint keep_alive):
        """
        Горячий путь каждого запроса.
        Роутинг → хэндлер → сериализация → write.
        """
        cdef object  handler, response
        cdef dict    params

        try:
            # роутинг; 404/405 — обычные хендлеры (бросают HTTPError), поэтому
            # проходят через middleware и error handlers, keep-alive сохраняется
            try:
                handler, params = self._router.match(request.method, request.path)
            except RouterNotFound:
                handler, params = self._app._h404, {}
            except MethodNotAllowed:
                handler, params = self._app._h405, {}

            # прокидываем контекст в request
            request.path_params = params
            request._app        = self._app
            request._transport  = self._transport

            # handler — bound `handle` хендлера, уже обёрнутый в middleware
            try:
                response = await handler(request)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task not in self._app._timed_out:
                    raise                      # отмена при остановке сервера
                self._app._timed_out.discard(task)
                task.uncancel()
                log.warning("handler timeout (%ss) in %s %s",
                            self._handler_timeout, request.method, request.path)
                self._write(_raw(_E503), keep_alive=False)
                return
            except HTTPError as exc:
                # ожидаемая ошибка из хендлера — обычный ответ, соединение живо
                response = exc.response(request)
            except Exception:
                log.exception("unhandled error in %s %s", request.method, request.path)
                self._write(_raw(_E500), keep_alive=False)
                return

            if not isinstance(response, Response):
                log.error("handler for %s %s returned %s, expected Response",
                          request.method, request.path, type(response).__name__)
                self._write(_raw(_E500), keep_alive=False)
                return

            self._write(response.render(keep_alive, request.method == "HEAD"), keep_alive)
        finally:
            self._inflight -= 1
            if (keep_alive and self._inflight == 0 and not self._receiving
                    and self._transport is not None):
                self._arm_idle()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _reset(self):
        """Подготовить к следующему запросу (keep-alive переиспользует соединение)."""
        self._request = Request()
        self._parser  = httptools.HttpRequestParser(self)
        self._headers_done = False
        self._hdr_bytes    = 0
        self._recv_bytes   = 0

    def _write(self, bytes data, bint keep_alive):
        if self._transport is None:
            return
        self._transport.write(data)
        if not keep_alive:
            self._transport.close()

    # ── таймеры и отказы ─────────────────────────────────────────────────────

    def _task_done(self, task):
        self._app._tasks.pop(task, None)
        self._app._timed_out.discard(task)    # отмену могли не успеть доставить

    def _arm(self, delay, int kind):
        """Дедлайн — просто число; таймеров на запрос нет, проверяет sweeper."""
        if delay is None:
            self._deadline = 0.0
        else:
            self._deadline = self._loop.time() + delay
            self._deadline_kind = kind

    def _arm_idle(self):
        self._arm(self._keepalive_timeout, _DL_IDLE)

    def _check_deadline(self, double now):
        """Вызывается sweeper'ом раз в интервал."""
        if self._deadline == 0.0 or now < self._deadline:
            return
        self._deadline = 0.0
        if self._deadline_kind == _DL_READ:
            self._fail(_E408)                      # запрос не дочитан (slowloris)
        elif self._inflight == 0 and not self._receiving:
            self.close()                           # keep-alive простаивает

    def _fail(self, tuple err):
        """Отправить отказ и закрыть соединение; дальше данные игнорируются."""
        self._deadline = 0.0
        self._reject = err
        if self._transport is not None:
            self._transport.write(_raw(err))
            self._transport.close()
