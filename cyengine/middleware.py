"""
Встроенные middleware.

Порядок: первый в app.use(...) — самый внешний. Рекомендуемый:

    app.use(access_log(), cors(allow_origins=["https://example.com"]), recover)

access_log снаружи видит всё (включая 404 и ответы error handlers); cors снаружи
recover, иначе 500 после исключения уйдёт без CORS-заголовков и браузер покажет
CORS-ошибку вместо настоящей.
"""

import logging
import re
from time import perf_counter

from cyengine.core._types import Response
from cyengine.errors import HTTPError
from cyengine.handler import HandlerFunc

log = logging.getLogger("cyengine")


# ─── recover ─────────────────────────────────────────────────────────────────

def recover(next):
    """HTTPError -> ответ с её статусом; любое другое исключение -> лог + 500."""

    async def handle(request):
        try:
            return await next.handle(request)
        except HTTPError as exc:
            return exc.response(request)
        except Exception:
            log.exception("unhandled error in %s %s", request.method, request.path)
            return Response.text("Internal Server Error", status=500)

    return HandlerFunc(handle)


# ─── access log ──────────────────────────────────────────────────────────────

def _default_access_logger() -> logging.Logger:
    """Логгер cyengine.access. Если logging никто не настраивал — печатает в stderr."""
    lg = logging.getLogger("cyengine.access")
    if not lg.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        lg.addHandler(handler)
        lg.setLevel(logging.INFO)
        lg.propagate = False
    return lg


def access_log(logger: logging.Logger = None, level: int = logging.INFO):
    """
    Одна строка на запрос:

        127.0.0.1 "GET /users/42?x=1" 200 25b 0.31ms

    Исключение, не обработанное ниже по цепочке, пишется как 500 и летит дальше.
    Если в приложении уже настроен logging (root-handler), пишет через него,
    иначе — в stderr.
    """
    lg = logger if logger is not None else _default_access_logger()

    def middleware(next):
        async def handle(request):
            start = perf_counter()
            try:
                response = await next.handle(request)
            except Exception:
                _emit(lg, level, request, 500, 0, start)
                raise
            _emit(lg, level, request, response.status, len(response.body), start)
            return response

        return HandlerFunc(handle)

    return middleware


def _emit(lg, level, request, status, size, start):
    if not lg.isEnabledFor(level):
        return
    client = request.client
    target = request.path
    if request.query_string:
        target += "?" + request.query_string.decode("latin-1")
    lg.log(level, '%s "%s %s" %d %db %.2fms',
           client[0] if client else "-", request.method, target,
           status, size, (perf_counter() - start) * 1000)


# ─── CORS ────────────────────────────────────────────────────────────────────

_DEFAULT_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")


def cors(allow_origins=None, *, allow_origin_regex: str = None,
         allow_methods=_DEFAULT_METHODS, allow_headers=None, expose_headers=(),
         allow_credentials: bool = False, max_age: int = 600):
    """
    CORS.

    allow_origins      — точные origin'ы ("https://example.com") или "*" для любых.
                         Не задано и нет allow_origin_regex → "*".
    allow_origin_regex — regex, которому должен целиком соответствовать Origin;
                         можно вместе со списком (подходит любой из способов).
    allow_methods      — что отвечать на preflight в Access-Control-Allow-Methods.
    allow_headers      — список разрешённых заголовков; None — разрешить всё,
                         что запросил браузер (эхо Access-Control-Request-Headers).
    expose_headers     — какие заголовки ответа доступны JS-коду клиента.
    allow_credentials  — cookies / Authorization. С "*" вместо звёздочки
                         отдаётся конкретный Origin (спецификация запрещает "*").
    max_age            — сколько секунд браузер кэширует preflight.

    Preflight (OPTIONS + Origin + Access-Control-Request-Method) отвечается здесь,
    без вызова хендлера. Запрещённый Origin на preflight → 403.
    На обычные запросы без Origin или с запрещённым Origin заголовки CORS не добавляются.
    """
    if allow_origins is None:
        allow_origins = () if allow_origin_regex else ("*",)
    origins = set(allow_origins)
    wildcard = "*" in origins
    regex = re.compile(allow_origin_regex) if allow_origin_regex else None
    methods = ", ".join(allow_methods)
    headers_list = ", ".join(allow_headers) if allow_headers is not None else None
    expose = ", ".join(expose_headers)
    # ACAO зависит от Origin, кроме случая «звёздочка для всех»
    vary_origin = not (wildcard and not allow_credentials)

    def origin_allowed(origin: str) -> bool:
        return wildcard or origin in origins or (
            regex is not None and regex.fullmatch(origin) is not None)

    def origin_value(origin: str) -> str:
        return "*" if wildcard and not allow_credentials else origin

    def middleware(next):
        async def handle(request):
            origin = request.header("origin")
            if not origin:
                response = await next.handle(request)
                if vary_origin:
                    _add_vary(response, "Origin")
                return response

            allowed = origin_allowed(origin)

            if (request.method == "OPTIONS"
                    and request.header("access-control-request-method")):
                return _preflight(request, origin, allowed)

            response = await next.handle(request)
            if vary_origin:
                _add_vary(response, "Origin")
            if allowed:
                h = response.headers
                h["Access-Control-Allow-Origin"] = origin_value(origin)
                if allow_credentials:
                    h["Access-Control-Allow-Credentials"] = "true"
                if expose:
                    h["Access-Control-Expose-Headers"] = expose
            return response

        def _preflight(request, origin, allowed):
            if not allowed:
                return HTTPError(403, "CORS origin not allowed").response(request)
            h = {
                "Access-Control-Allow-Origin": origin_value(origin),
                "Access-Control-Allow-Methods": methods,
                "Access-Control-Max-Age": str(max_age),
            }
            requested = request.header("access-control-request-headers")
            if headers_list is not None:
                h["Access-Control-Allow-Headers"] = headers_list
            elif requested:
                h["Access-Control-Allow-Headers"] = requested
            if allow_credentials:
                h["Access-Control-Allow-Credentials"] = "true"
            response = Response.empty(204, headers=h)
            _add_vary(response, "Origin, Access-Control-Request-Method, "
                                "Access-Control-Request-Headers")
            return response

        return HandlerFunc(handle)

    return middleware


def _add_vary(response, value: str) -> None:
    """Дописать к Vary, не затирая то, что поставил хендлер (ключ — без учёта регистра)."""
    headers = response.headers
    for key in headers:
        if key.lower() == "vary":
            existing = headers[key]
            missing = [v for v in value.split(", ")
                       if v.lower() not in {p.strip().lower() for p in existing.split(",")}]
            if missing:
                headers[key] = existing + ", " + ", ".join(missing)
            return
    headers["Vary"] = value
