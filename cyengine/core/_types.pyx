# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: nonecheck=False

import orjson

from libc.string cimport memchr



class CyEngineError(Exception):
    """Базовый класс всех ошибок фреймворка."""


class ProviderNotRegistered(CyEngineError):
    def __init__(self, cls: type) -> None:
        super().__init__(
            f"Provider '{cls.__name__}' is not registered. "
            f"Call app.provide({cls.__name__}, ...) before app.run()."
        )
        self.cls = cls


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
        if b"=" in pair:
            k, _, v = pair.partition(b"=")
            result[k.decode("utf-8", errors="replace")] = v.decode("utf-8", errors="replace")
        elif pair:
            result[pair.decode("utf-8", errors="replace")] = ""

    return result


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
    cdef public bytearray _body
    cdef        dict      _query      # None до первого .query
    cdef public object    _app
    cdef public object    _transport

    def __cinit__(self):
        self.method       = ""
        self.path         = ""
        self.query_string = b""
        self.headers      = {}
        self.path_params  = {}
        self._body        = bytearray()
        self._query       = None
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

    # ── тело ─────────────────────────────────────────────────────────────────

    cpdef bytes body(self):
        """Сырое тело запроса."""
        return bytes(self._body)

    cpdef object json(self):
        """Десериализовать тело как JSON через orjson."""
        return orjson.loads(self._body)

    cpdef str text(self, str encoding="utf-8"):
        """Тело как строка."""
        return self._body.decode(encoding)

    # ── провайдеры (DI) ──────────────────────────────────────────────────────

    cpdef object provide(self, object cls):
        """
        Получить зарегистрированный провайдер по типу.

        Usage:
            db = request.provide(Database)
            cache = request.provide(Cache)
        """
        if self._app is None:
            raise ProviderNotRegistered(cls)
        return self._app._get_provider(cls)

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
        return cls(body=orjson.dumps(data), status=status, headers=h)

    @classmethod
    def text(cls, str data, int status = 200, dict headers = None):
        cdef dict h = headers if headers is not None else {}
        h.setdefault("Content-Type", "text/plain; charset=utf-8")
        return cls(body=data.encode("utf-8"), status=status, headers=h)

    @classmethod
    def empty(cls, int status = 204):
        return cls(body=b"", status=status)

    cpdef bytes render(self, bint keep_alive = True):
        """
        Сериализовать в HTTP/1.1 wire-формат.
        Вызывается один раз на запрос протоколом.
        """
        cdef bytearray buf
        cdef bytes     status_line
        cdef bytes     cl_header

        # status line — из prealloc dict, O(1) lookup
        status_line = _STATUS_LINES.get(
            self.status,
            b"HTTP/1.1 " + str(self.status).encode() + _CRLF
        )

        cl_header = b"Content-Length: " + str(len(self.body)).encode() + _CRLF

        buf  = bytearray(status_line)
        buf += _SERVER
        buf += _CONN_KA if keep_alive else _CONN_CL
        buf += cl_header

        # пользовательские заголовки
        cdef str k, v
        for k, v in self.headers.items():
            buf += k.encode("latin-1") + b": " + v.encode("latin-1") + _CRLF

        buf += _CRLF

        if self.body:
            buf += self.body

        return bytes(buf)

    def __repr__(self):
        return f"<Response {self.status} {len(self.body)}b>"
