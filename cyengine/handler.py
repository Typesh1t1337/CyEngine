"""
Контракт хендлера — то, с чем работает фреймворк.

    class UserHandler:
        def __init__(self, db):
            self.db = db

        async def handle(self, request: Request) -> Response:
            user = await self.db.get(request.path_params["id"])
            return Response.json(user)

Обычная `async def f(request) -> Response` тоже подходит —
фреймворк оборачивает её в HandlerFunc (как http.HandlerFunc в Go).

Middleware — функция Handler -> Handler:

    def timing(next):
        async def handle(request):
            t = time.perf_counter()
            resp = await next.handle(request)
            resp.headers["X-Time"] = str(time.perf_counter() - t)
            return resp
        return HandlerFunc(handle)
"""

import inspect
from typing import Callable, Iterable, Protocol, runtime_checkable


@runtime_checkable
class Handler(Protocol):
    async def handle(self, request): ...


Middleware = Callable[[Handler], Handler]


class HandlerFunc:
    """Адаптер: async-функция -> Handler. Без лишнего await на горячем пути."""

    __slots__ = ("handle",)

    def __init__(self, fn):
        self.handle = fn


def as_handler(obj) -> Handler:
    """Привести объект к Handler. Падает на регистрации, а не на запросе."""
    handle = getattr(obj, "handle", None)
    if handle is not None and callable(handle):
        result, target = obj, handle
    elif callable(obj):
        result, target = HandlerFunc(obj), obj
    else:
        raise TypeError(
            f"{obj!r} is not a handler: expected an object with "
            f"`async def handle(request)` or an async function"
        )

    if not inspect.iscoroutinefunction(target):
        raise TypeError(f"handler {obj!r} must be async (`async def handle(request)`)")
    return result


def chain(handler: Handler, middleware: Iterable[Middleware]) -> Handler:
    """Обернуть handler в middleware. Первый в списке — самый внешний."""
    for mw in reversed(list(middleware)):
        handler = as_handler(mw(handler))
    return handler
