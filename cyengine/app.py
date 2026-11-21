from cyengine.core._router import Router
from cyengine.core._protocol import HttpProtocol
from cyengine.handler import HandlerFunc, chain
from cyengine.routing import Routes
from cyengine.limits import Limits
from cyengine.core._types import Response
from cyengine.errors import ErrorHandlers, HTTPError, NotFound

from cyengine import server as _server


class App(Routes):
    """
    Регистрация хендлеров и middleware + запуск. Зависимости живут в main():

        async def main():
            db = await Database.connect(dsn)
            try:
                app = App()
                app.use(recover)
                app.handle("GET", "/users/<int:id>", UserHandler(db))
                await app.serve(port=8000)
            finally:
                await db.close()
    """

    def __init__(self, limits: Limits = None):
        self._limits = limits if limits is not None else Limits()
        self._routes: list = []         # (method, полный путь, Handler, Group | None)
        self._middleware: list = []
        self._errors = ErrorHandlers()
        self._router = None             # собирается в serve()
        self._h404 = None               # хендлеры 404/405, тоже собираются в serve()
        self._h405 = None
        self._tasks: dict = {}          # task запроса → дедлайн хендлера (0 — нет)
        self._timed_out: set = set()    # задачи, отменённые по handler_timeout
        self._protocols: set = set()    # открытые соединения

    # ── регистрация ──────────────────────────────────────────────────────────

    def _add(self, methods, path, handler):
        for method in methods:
            self._routes.append((method, path, handler, None))

    def _group_context(self):
        return self, "", None

    def error_handler(self, exc_type, fn=None):
        """
        Что вернуть клиенту, если хендлер бросил исключение этого типа.

            async def not_found(request, exc):
                return Response.json({"error": str(exc)}, status=404)

            app.error_handler(UserNotFound, not_found)

            @app.error_handler(ValidationError)      # декоратор
            async def invalid(request, exc): ...

        Подходит и для подклассов; побеждает самый специфичный тип.
        error_handler(Exception, fn) — перехват всего остального вместо 500.
        """
        if fn is not None:
            self._errors.add(exc_type, fn)
            return self

        def decorator(func):
            self._errors.add(exc_type, func)
            return func
        return decorator

    # ── запуск ───────────────────────────────────────────────────────────────

    def _build_router(self) -> Router:
        """Цепочки middleware собираются один раз, на старте."""
        router = Router()
        for method, path, handler, group in self._routes:
            mw = [*self._middleware, *(group._all_middleware() if group else ())]
            router.add(method, path, chain(self._errors.wrap(handler), mw).handle)

        # OPTIONS: если для пути нет своего хендлера — автоответ 204 + Allow.
        # Идёт через ту же цепочку (в т.ч. middleware группы), так что CORS и т.п. его видят.
        async def options(request):
            allow = ", ".join(router.allowed(request.path))
            return Response.empty(204, headers={"Allow": allow})

        options_wrapped = self._errors.wrap(HandlerFunc(options))
        auto_cache: dict = {}                # одна цепочка на набор middleware
        by_path: dict = {}
        for method, path, _, group in self._routes:
            entry = by_path.setdefault(path, [set(), group])
            entry[0].add(method)
        for path, (methods, group) in by_path.items():
            if "OPTIONS" in methods:
                continue
            mw = [*self._middleware, *(group._all_middleware() if group else ())]
            key = tuple(id(m) for m in mw)
            if key not in auto_cache:
                auto_cache[key] = chain(options_wrapped, mw).handle
            router.add("OPTIONS", path, auto_cache[key])

        # 404/405 — такие же хендлеры: middleware и error handlers на них действуют
        async def not_found(request):
            raise NotFound()

        async def method_not_allowed(request):
            allow = ", ".join(router.allowed(request.path))
            raise HTTPError(405, headers={"Allow": allow})

        for name, fn in (("_h404", not_found), ("_h405", method_not_allowed)):
            wrapped = self._errors.wrap(HandlerFunc(fn))
            setattr(self, name, chain(wrapped, self._middleware).handle)
        return router

    def _make_protocol(self, loop):
        return HttpProtocol(self._router, self, loop)

    async def serve(self, host: str = "0.0.0.0", port: int = 8000,
                    grace: float = 10.0):
        """Блокируется до SIGINT/SIGTERM, затем дожидается текущих запросов."""
        await _server.serve(self, host, port, grace=grace)

    def run(self, host: str = "0.0.0.0", port: int = 8000):
        """Sync-вариант serve() — для приложений, которым нечего закрывать."""
        _server.run(self, host=host, port=port)


__all__ = ["App"]
