from cyengine.core._types import Request, Response, CyEngineError
from cyengine.core._router import Router
from cyengine.core._protocol import HttpProtocol

from cyengine.server import run as _run

class App:
    def __init__(self):
        self._router = Router()
        self.providers = {}

    def get(self, path: str):
        def decorator(func):
            self._router.add("GET", path, func)
            return func

        return decorator

    def put(self, path: str):
        def decorator(func):
            self._router.add("PUT", path, func)
            return func

        return decorator

    def post(self, path: str):
        def decorator(func):
            self._router.add("POST", path, func)
            return func

        return decorator

    def delete(self, path: str):
        def decorator(func):
            self._router.add("DELETE", path, func)

            return func

        return decorator

    def patch(self, path: str):
        def decorator(func):
            self._router.add("PATCH", path, func)
            return func

        return decorator

    def route(self, path: str, methods: list[str]):
        def decorator(func):
            for method in methods:
                self._router.add(method, path, func)

            return func

        return decorator

    def provider(self, cls, instance):
        """
        Зарегистрировать провайдер.

            app.provide(Database, Database("postgresql://..."))
            app.provide(Cache, RedisCache("redis://..."))

        Внутри хэндлера:
            db = request.provide(Database)
        """

        self._providers[cls] = instance
        return self

    def _get_provider(self, cls):
        from cyengine.core._types import ProviderNotRegistered
        try:
            return self._providers[cls]
        except KeyError:
            raise ProviderNotRegistered(cls)

    def _make_protocol(self, loop):
        return HttpProtocol(self._router, self, loop)

    def run(self, host: str = "0.0.0.0", port: int = 8000, workers: int = 1):
        _run(self, host=host, port=port, workers=workers)


__all__ = ["App", "Request", "Response"]

