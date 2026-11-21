# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: nonecheck=False

import time
from email.utils import formatdate
from http import HTTPStatus
from urllib.parse import unquote_plus

import orjson

from libc.string cimport memchr



class CyEngineError(Exception):
    """Базовый класс всех ошибок фреймворка."""


class RouterNotFound(CyEngineError):
    def __init__(self, method: str, path: str) -> None:
        super().__init__(f"{method} {path}")
        self.method  = method
        self.path    = path


class MethodNotAllowed(CyEngineError):
    def __init__(self, method: str, path: str) -> None:
        super().__init__(f"{method} {path}")
        self.method  = method
        self.path    = path


cdef str _decode_qs(bytes part):
    """%xx и '+' → символы; fast path для частей без экранирования."""
    cdef str s = part.decode("utf-8", errors="replace")
    if b"%" in part or b"+" in part:
        return unquote_plus(s)
    return s


cdef dict _parse_query(bytes qs):
    """
    Парсинг query string на C уровне.
    b"foo=1&bar=2" → {"foo": "1", "bar": "2"}
    Вызывается лениво — только если юзер обращается к request.query.
    """
    if not qs:
        return {}

    cdef dict   result = {}
    cdef list   pairs  = qs.split(b"&")
    cdef bytes  pair, k, v
    cdef int    sep

    for pair in pairs:
        if not pair:
            continue
        k, _, v = pair.partition(b"=")
        result[_decode_qs(k)] = _decode_qs(v)

    return result


cdef dict _ADAPTERS = {}


cdef object _adapter(object model):
    """TypeAdapter на модель — один раз, дальше из кэша."""
    ta = _ADAPTERS.get(model)
    if ta is None:
        try:
            from pydantic import TypeAdapter
        except ImportError:
            raise ImportError("request.parse() needs pydantic: pip install pydantic") from None
        ta = TypeAdapter(model)
        _ADAPTERS[model] = ta
    return ta


cdef object _jsonable(object value):
    try:
        orjson.dumps(value)
        return value
    except TypeError:
        return repr(value)


cdef object _invalid(object exc, str source):
    """pydantic.ValidationError → RequestValidationError (422). Холодный путь."""
    from cyengine.errors import RequestValidationError
    return RequestValidationError([
        {"type": e["type"], "loc": [source] + list(e["loc"]), "msg": e["msg"],
         "input": _jsonable(e.get("input"))}
        for e in exc.errors(include_url=False, include_context=False)
    ])


cdef bytes _dumps(object data):
    """
    Объект → JSON bytes. dict/list — orjson; модели pydantic (и контейнеры с
    моделями) — pydantic-core пишет прямо в bytes, без промежуточного dict.
    """
    cdef object ser
    if type(data) is dict or type(data) is list:
        try:
            return orjson.dumps(data)
        except TypeError:
            from pydantic_core import to_json     # внутри есть модель
            return to_json(data, by_alias=True)
    ser = getattr(type(data), "__pydantic_serializer__", None)
    if ser is not None:
        return ser.to_json(data, by_alias=True)
    try:
        return orjson.dumps(data)
    except TypeError:
        from pydantic_core import to_json
        return to_json(data, by_alias=True)


# ─── Request ─────────────────────────────────────────────────────────────────

cdef class Request:
    """
    HTTP Request — cdef класс.

    Поля объявлены как cdef → хранятся в C struct, не в __dict__.
    Доступ к ним быстрее чем к атрибутам обычного Python объекта.

    headers хранятся как dict[str, str] — decode происходит один раз
    при парсинге в HttpProtocol, не при каждом вызове .header().

    _query = None до первого обращения к .query (ленивый парсинг).
    """

    # публичные — доступны юзеру напрямую
    cdef public str    method
    cdef public str    path
    cdef public bytes  query_string
    cdef public dict   headers        # str → str, decoded once
    cdef public dict   path_params    # str → Any, typed by router

    # приватные — инфраструктура протокола
    cdef public object    _body       # bytes (один кусок, без копии) | bytearray
    cdef        dict      _query      # None до первого .query
    cdef        dict      _state      # None до первого .state
    cdef public object    _app
    cdef public object    _transport

    def __cinit__(self):
        self.method       = ""
        self.path         = ""
        self.query_string = b""
        self.headers      = {}
        self.path_params  = {}
        self._body        = b""
        self._query       = None
        self._state       = None
        self._app         = None
        self._transport   = None

    # ── заголовки ────────────────────────────────────────────────────────────

    cpdef str header(self, str name, str default=""):
        """
        Получить заголовок по имени (case-insensitive).
        Имена хранятся в нижнем регистре — lowercase один раз при парсинге.
        """
        return self.headers.get(name.lower(), default)

    # ── query string ─────────────────────────────────────────────────────────

    @property
    def query(self) -> dict:
        """
        Ленивый парсинг query string.
        /users?page=2&limit=10 → {"page": "2", "limit": "10"}
        Парсится только при первом обращении.
        """
        if self._query is None:
            self._query = _parse_query(self.query_string)
        return self._query

    # ── клиент ───────────────────────────────────────────────────────────────

    @property
    def client(self):
        """Адрес клиента: (host, port), или None, если соединение уже закрыто."""
        if self._transport is None:
            return None
        return self._transport.get_extra_info("peername")

    # ── state ────────────────────────────────────────────────────────────────

    @property
    def state(self) -> dict:
        """
        Произвольные данные запроса — для передачи из middleware в хендлер.

            request.state["user"] = user        # в middleware
            user = request.state["user"]        # в хендлере
        """
        if self._state is None:
            self._state = {}
        return self._state

    # ── тело ─────────────────────────────────────────────────────────────────

    cpdef bytes body(self):
        """Сырое тело запроса."""
        return bytes(self._body)

    cpdef object json(self):
        """
        Десериализовать тело как JSON через orjson.
        Невалидный или пустой JSON → BadRequest (400).
        """
        try:
            return orjson.loads(self._body)
        except orjson.JSONDecodeError:
            from cyengine.errors import BadRequest
            raise BadRequest("invalid JSON body") from None

    cpdef object parse(self, object model):
        """
        Тело (JSON) → экземпляр pydantic-модели (или любого типа, который понимает
        TypeAdapter: list[Model], dict[str, int]). Парсит и валидирует за один проход
        в Rust (validate_json): промежуточных dict/list нет, тело не копируется.
        Не прошло → RequestValidationError (422).

            user = request.parse(CreateUser)
        """
        ta = _adapter(model)
        try:
            return ta.validate_json(self._body)
        except Exception as exc:
            from pydantic import ValidationError
            if isinstance(exc, ValidationError):
                raise _invalid(exc, "body") from None
            raise

    cpdef object parse_query(self, object model):
        """Query string → модель (значения-строки приводятся к типам полей)."""
        ta = _adapter(model)
        try:
            return ta.validate_python(self.query)
        except Exception as exc:
            from pydantic import ValidationError
            if isinstance(exc, ValidationError):
                raise _invalid(exc, "query") from None
            raise

    cpdef str text(self, str encoding="utf-8"):
        """Тело как строка. Не декодируется → BadRequest (400)."""
        try:
            return self._body.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            from cyengine.errors import BadRequest
            raise BadRequest(f"body is not valid {encoding}") from None

    # ── repr ─────────────────────────────────────────────────────────────────

    def __repr__(self):
        return f"<Request {self.method} {self.path}>"


# ─── Response ────────────────────────────────────────────────────────────────

# Преаллоцированные строки статусов — не аллоцируем на каждый запрос
cdef dict _STATUS_LINES = {
    200: b"HTTP/1.1 200 OK\r\n",
    201: b"HTTP/1.1 201 Created\r\n",
    204: b"HTTP/1.1 204 No Content\r\n",
    400: b"HTTP/1.1 400 Bad Request\r\n",
    401: b"HTTP/1.1 401 Unauthorized\r\n",
    403: b"HTTP/1.1 403 Forbidden\r\n",
    404: b"HTTP/1.1 404 Not Found\r\n",
    405: b"HTTP/1.1 405 Method Not Allowed\r\n",
    409: b"HTTP/1.1 409 Conflict\r\n",
    422: b"HTTP/1.1 422 Unprocessable Entity\r\n",
    429: b"HTTP/1.1 429 Too Many Requests\r\n",
    500: b"HTTP/1.1 500 Internal Server Error\r\n",
    502: b"HTTP/1.1 502 Bad Gateway\r\n",
    503: b"HTTP/1.1 503 Service Unavailable\r\n",
}

cdef long _date_sec = 0
cdef bytes _date_line = b""


cpdef bytes date_header():
    """Строка `Date: ...\\r\\n`; форматируется раз в секунду, а не на каждый ответ."""
    global _date_sec, _date_line
    cdef long now = <long>time.time()
    if now != _date_sec:
        _date_sec = now
        _date_line = b"Date: " + formatdate(now, usegmt=True).encode() + b"\r\n"
    return _date_line


def _reason(int status):
    try:
        return HTTPStatus(status).phrase.encode()
    except ValueError:
        return b"Unknown"


cdef dict _HEADER_KEYS = {}


cdef bytes _SERVER   = b"Server: cyframe\r\n"
cdef bytes _CONN_KA  = b"Connection: keep-alive\r\n"
cdef bytes _CONN_CL  = b"Connection: close\r\n"
cdef bytes _CRLF     = b"\r\n"


cdef class Response:
    """
    HTTP Response — cdef класс.

    headers хранятся как dict[str, str] — encode при render(),
    вызывается один раз на запрос.
    """
    cdef public int    status
    cdef public bytes  body
    cdef public dict   headers    # str → str
    cdef public bint   keep_alive

    def __cinit__(self,
                  bytes body      = b"",
                  int   status    = 200,
                  dict  headers   = None,
                  bint  keep_alive = True):
        self.status     = status
        self.body       = body
        self.headers    = headers if headers is not None else {}
        self.keep_alive = keep_alive

    @classmethod
    def json(cls, object data, int status = 200, dict headers = None):
        """
        Быстрый конструктор JSON ответа.

        Usage:
            return Response.json({"id": 1})
            return Response.json({"error": "not found"}, status=404)
        """
        cdef dict h = headers if headers is not None else {}
        h.setdefault("Content-Type", "application/json")
        return cls(body=_dumps(data), status=status, headers=h)

    @classmethod
    def text(cls, str data, int status = 200, dict headers = None):
        cdef dict h = headers if headers is not None else {}
        h.setdefault("Content-Type", "text/plain; charset=utf-8")
        return cls(body=data.encode("utf-8"), status=status, headers=h)

    @classmethod
    def empty(cls, int status = 204, dict headers = None):
        return cls(body=b"", status=status, headers=headers)

    cpdef bytes render(self, bint keep_alive = True, bint head = False):
        """
        Сериализовать в HTTP/1.1 wire-формат.
        Вызывается один раз на запрос протоколом.

        head=True (ответ на HEAD): заголовки как для GET, включая
        Content-Length, но без тела.
        """
        cdef bytearray buf
        cdef bytes     status_line
        cdef bytes     cl_header

        # status line — из prealloc dict, O(1) lookup
        status_line = _STATUS_LINES.get(self.status)
        if status_line is None:      # редкий код: строку собираем (раньше собиралась всегда)
            status_line = (b"HTTP/1.1 " + str(self.status).encode() + b" "
                           + _reason(self.status) + _CRLF)

        # у 1xx / 204 / 304 тела нет и Content-Length слать нельзя
        if self.status < 200 or self.status == 204 or self.status == 304:
            cl_header = b""
        else:
            cl_header = b"Content-Length: " + str(len(self.body)).encode() + _CRLF

        buf  = bytearray(status_line)
        buf += _SERVER
        buf += date_header()
        buf += _CONN_KA if keep_alive else _CONN_CL
        buf += cl_header

        # пользовательские заголовки
        cdef str k, v
        cdef bytes kline
        for k, v in self.headers.items():
            kline = _HEADER_KEYS.get(k)      # "Content-Type" → b"Content-Type: " один раз
            if kline is None:
                kline = k.encode("latin-1") + b": "
                if len(_HEADER_KEYS) < 256:
                    _HEADER_KEYS[k] = kline
            buf += kline
            buf += v.encode("latin-1")
            buf += _CRLF

        buf += _CRLF

        if self.body and not head:
            buf += self.body

        return bytes(buf)

    def __repr__(self):
        return f"<Response {self.status} {len(self.body)}b>"
