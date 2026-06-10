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
import httptools

from cyengine.core._types import (
    Request, Response,
    RouterNotFound, MethodNotAllowed, CyEngineError,
)
from cyengine.core._router import Router


# ─── Преаллоцированные ответы на типовые ошибки ──────────────────────────────
# Создаются один раз при импорте модуля, не на каждый запрос

cdef bytes _R404 = (
    b"HTTP/1.1 404 Not Found\r\n"
    b"Server: cyframe\r\nContent-Length: 9\r\nConnection: close\r\n\r\nNot Found"
)
cdef bytes _R405 = (
    b"HTTP/1.1 405 Method Not Allowed\r\n"
    b"Server: cyframe\r\nContent-Length: 18\r\nConnection: close\r\n\r\nMethod Not Allowed"
)
cdef bytes _R500 = (
    b"HTTP/1.1 500 Internal Server Error\r\n"
    b"Server: cyframe\r\nContent-Length: 21\r\nConnection: close\r\n\r\nInternal Server Error"
)
cdef bytes _R400 = (
    b"HTTP/1.1 400 Bad Request\r\n"
    b"Server: cyframe\r\nContent-Length: 11\r\nConnection: close\r\n\r\nBad Request"
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
        "_loop",        # event loop — для create_task
    )

    def __init__(self, router, app, loop):
        self._router    = router
        self._app       = app
        self._loop      = loop
        self._transport = None
        self._parser    = None
        self._request   = None

    # ── asyncio callbacks ────────────────────────────────────────────────────

    def connection_made(self, transport):
        self._transport = transport
        self._reset()

    def connection_lost(self, exc):
        self._transport = None

    def data_received(self, data: bytes):
        try:
            self._parser.feed_data(data)
        except httptools.HttpParserError:
            if self._transport:
                self._transport.write(_R400)
                self._transport.close()

    # ── httptools callbacks (вызываются синхронно из feed_data) ──────────────

    def on_url(self, url: bytes):
        cdef object req = self._request
        if b"?" in url:
            decoded = url.decode("ascii", errors="replace")
            idx = decoded.index("?")
            req.path         = decoded[:idx]
            req.query_string = url[idx + 1:]   # bytes — парсится лениво
        else:
            req.path = url.decode("ascii", errors="replace")

    def on_header(self, name: bytes, value: bytes):
        # lowercase один раз здесь — header() не делает лишних операций
        self._request.headers[
            name.decode("latin-1").lower()
        ] = value.decode("latin-1")

    def on_headers_complete(self):
        self._request.method = self._parser.get_method().decode("ascii")

    def on_body(self, body: bytes):
        self._request._body += body

    def on_message_complete(self):
        cdef bint keep_alive = self._parser.should_keep_alive()
        self._loop.create_task(self._handle(self._request, keep_alive))
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
        cdef bytes   raw

        # роутинг — бросает RouterNotFound / MethodNotAllowed
        try:
            handler, params = self._router.match(request.method, request.path)
        except RouterNotFound:
            self._write(_R404, keep_alive=False)
            return
        except MethodNotAllowed:
            self._write(_R405, keep_alive=False)
            return

        # прокидываем контекст в request
        request.path_params = params
        request._app        = self._app
        request._transport  = self._transport

        # вызов хэндлера
        try:
            response = await handler(request, **params)
        except CyEngineError:
            raise   # пусть летит наверх — это баг фреймворка
        except Exception:
            self._write(_R500, keep_alive=False)
            return

        # сериализация и отправка
        raw = _serialize(response, keep_alive)
        self._write(raw, keep_alive)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _reset(self):
        """Подготовить к следующему запросу (keep-alive переиспользует соединение)."""
        self._request = Request()
        self._parser  = httptools.HttpRequestParser(self)

    def _write(self, bytes data, bint keep_alive):
        if self._transport is None:
            return
        self._transport.write(data)
        if not keep_alive:
            self._transport.close()


# ─── Сериализация ответа ──────────────────────────────────────────────────────

cdef bytes _serialize(object response, bint keep_alive):
    """
    Привести ответ хэндлера к байтам.

    Хэндлер может вернуть:
      Response          → response.render(keep_alive)
      bytes             → обернуть в минимальный HTTP/1.1
      dict / list       → orjson + обернуть
    """
    if isinstance(response, Response):
        return response.render(keep_alive)

    if isinstance(response, bytes):
        return _wrap(response, b"application/octet-stream", keep_alive)

    # fallback — любой объект сериализуем через orjson
    import orjson
    return _wrap(orjson.dumps(response), b"application/json", keep_alive)


cdef bytes _HEADERS_TMPL = (
    b"HTTP/1.1 200 OK\r\n"
    b"Server: cyframe\r\n"
)
cdef bytes _KA   = b"Connection: keep-alive\r\n"
cdef bytes _CL   = b"Connection: close\r\n"
cdef bytes _CRLF = b"\r\n"


cdef bytes _wrap(bytes body, bytes content_type, bint keep_alive):
    """Минимальный HTTP/1.1 wrapper для bytes / orjson fallback."""
    cdef bytes conn = _KA if keep_alive else _CL
    cdef bytes cl   = b"Content-Length: " + str(len(body)).encode() + _CRLF
    cdef bytes ct   = b"Content-Type: "   + content_type + _CRLF
    return _HEADERS_TMPL + conn + ct + cl + _CRLF + body
