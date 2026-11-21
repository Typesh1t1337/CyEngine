"""
HTTP-ошибки: бросаешь из хендлера — фреймворк превращает в ответ.

    async def handle(self, request):
        user = await self.db.get(request.path_params["id"])
        if user is None:
            raise NotFound("user not found")
        ...

Тело ответа — JSON `{"error": <detail>}`; если клиент просит text/plain
(и не просит application/json) — обычный текст.
"""

import inspect
from http import HTTPStatus

import orjson

from cyengine.core._types import CyEngineError, Response
from cyengine.handler import HandlerFunc


class HTTPError(CyEngineError):
    """
    Базовая HTTP-ошибка.

        raise HTTPError(418, "I'm a teapot")
        raise HTTPError(429, {"retry_after": 5}, headers={"Retry-After": "5"})

    detail — строка или любой JSON-сериализуемый объект;
    по умолчанию — стандартная фраза статуса.
    """

    def __init__(self, status: int, detail=None, *, headers: dict = None):
        if not 400 <= status <= 599:
            raise ValueError(f"HTTPError status must be 4xx or 5xx, got {status}")
        if detail is None:
            detail = _phrase(status)
        super().__init__(f"{status} {detail}")
        self.status = status
        self.detail = detail
        self.headers = dict(headers) if headers else {}

    def response(self, request=None) -> Response:
        """Собрать Response; формат тела выбирается по заголовку Accept."""
        headers = dict(self.headers)
        if _wants_text(request):
            text = self.detail if isinstance(self.detail, str) \
                else orjson.dumps(self.detail).decode()
            return Response.text(text, status=self.status, headers=headers)
        return Response.json({"error": self.detail}, status=self.status, headers=headers)


def _phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"


def _wants_text(request) -> bool:
    if request is None:
        return False
    accept = request.header("accept")
    return "text/plain" in accept and "application/json" not in accept


class BadRequest(HTTPError):
    def __init__(self, detail=None, *, headers=None):
        super().__init__(400, detail, headers=headers)


class Unauthorized(HTTPError):
    def __init__(self, detail=None, *, headers=None):
        super().__init__(401, detail, headers=headers)


class Forbidden(HTTPError):
    def __init__(self, detail=None, *, headers=None):
        super().__init__(403, detail, headers=headers)


class NotFound(HTTPError):
    def __init__(self, detail=None, *, headers=None):
        super().__init__(404, detail, headers=headers)


class Conflict(HTTPError):
    def __init__(self, detail=None, *, headers=None):
        super().__init__(409, detail, headers=headers)


class UnprocessableEntity(HTTPError):
    def __init__(self, detail=None, *, headers=None):
        super().__init__(422, detail, headers=headers)


class RequestValidationError(HTTPError):
    """
    422: вход не прошёл валидацию pydantic (request.parse / request.parse_query).
    detail — список [{"type", "loc", "msg", "input"}, ...]; loc начинается с
    источника: ("body", "age"). Формат можно заменить через app.error_handler.
    """

    def __init__(self, errors: list):
        super().__init__(422, errors)
        self.errors = errors


# ─── Обработчики ошибок ──────────────────────────────────────────────────────

async def _http_error_handler(request, exc):
    return exc.response(request)


class ErrorHandlers:
    """
    Реестр «тип исключения → функция». Функция: `async def f(request, exc) -> Response`.

    Выбирается самый специфичный зарегистрированный тип (по MRO исключения).
    По умолчанию зарегистрирован только HTTPError → exc.response(request);
    его можно переопределить. Исключение без обработчика летит дальше
    (в recover / протокол → 500).
    """

    def __init__(self):
        self._map = {HTTPError: _http_error_handler}

    def add(self, exc_type, fn) -> None:
        if not (isinstance(exc_type, type) and issubclass(exc_type, Exception)):
            raise TypeError(f"error_handler: {exc_type!r} is not an Exception subclass")
        call = fn if inspect.isfunction(fn) or inspect.ismethod(fn) else getattr(fn, "__call__", None)
        if not callable(fn) or not inspect.iscoroutinefunction(call):
            raise TypeError(
                f"error handler {fn!r} must be async: `async def f(request, exc) -> Response`"
            )
        self._map[exc_type] = fn

    def find(self, exc: BaseException):
        for cls in type(exc).__mro__:
            fn = self._map.get(cls)
            if fn is not None:
                return fn
        return None

    def wrap(self, handler):
        """Обернуть Handler: исключение → ответ. Ставится ближе всего к хендлеру,
        поэтому middleware видят уже готовый Response."""
        find = self.find

        async def handle(request):
            try:
                return await handler.handle(request)
            except Exception as exc:
                fn = find(exc)
                if fn is None:
                    raise
                return await fn(request, exc)

        return HandlerFunc(handle)
