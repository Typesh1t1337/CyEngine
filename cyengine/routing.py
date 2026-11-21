"""Регистрация роутов: общий интерфейс для App и Group."""

from cyengine.handler import Middleware, as_handler


def join_path(prefix: str, path: str) -> str:
    """join_path("/api/", "/users/") -> "/api/users";  join_path("/api", "/") -> "/api"."""
    parts = [s for s in (prefix.strip("/"), path.strip("/")) if s]
    return "/" + "/".join(parts)


class Routes:
    """
    Миксин: handle / get / post / ... / use / group.
    Наследник задаёт `_middleware` (list), `_add()` и `_group_context()`.
    """

    _middleware: list

    def use(self, *middleware: Middleware):
        """Первый переданный — самый внешний. Действует на все роуты (в том числе
        зарегистрированные раньше — цепочки собираются на старте)."""
        self._middleware.extend(middleware)
        return self

    def handle(self, methods, path: str, handler):
        """
        app.handle("GET", "/users/<int:id>", UserHandler(db))
        api.handle(["GET", "HEAD"], "/health", health)
        """
        h = as_handler(handler)
        if isinstance(methods, str):
            methods = [methods]
        self._add([m.upper() for m in methods], path, h)
        return self

    def group(self, prefix: str, *middleware: Middleware) -> "Group":
        """
        Группа роутов с общим префиксом и своими middleware.

            api = app.group("/api/v1", auth)
            api.handle("GET", "/users/<int:id>", UserHandler(db))   # GET /api/v1/users/<id>
            admin = api.group("/admin", require_admin)               # вложенная

        Порядок цепочки: middleware приложения → родительских групп → этой группы →
        error handlers → хендлер. 404/405 внутри префикса проходят только через
        middleware приложения: до выбора роута неизвестно, какой группе они принадлежат.
        """
        if not isinstance(prefix, str) or (prefix not in ("", "/") and not prefix.startswith("/")):
            raise ValueError(f'group prefix must start with "/", got {prefix!r}')
        app, base, parent = self._group_context()
        return Group(app, join_path(base, prefix), parent, list(middleware))

    # сахар для функций-хендлеров: @app.get("/path")

    def route(self, path: str, methods: list):
        def decorator(func):
            self.handle(methods, path, func)
            return func
        return decorator

    def get(self, path: str):
        return self.route(path, ["GET"])

    def post(self, path: str):
        return self.route(path, ["POST"])

    def put(self, path: str):
        return self.route(path, ["PUT"])

    def delete(self, path: str):
        return self.route(path, ["DELETE"])

    def patch(self, path: str):
        return self.route(path, ["PATCH"])

    # реализуют App и Group

    def _add(self, methods, path, handler):
        raise NotImplementedError

    def _group_context(self):
        raise NotImplementedError


class Group(Routes):
    """Роуты под общим префиксом. Создаётся через app.group(...) / group.group(...)."""

    def __init__(self, app, prefix: str, parent, middleware: list):
        self._app = app
        self._prefix = prefix
        self._parent = parent
        self._middleware = middleware

    def _all_middleware(self) -> list:
        """Снаружи внутрь: сначала родительские группы, затем эта."""
        outer = self._parent._all_middleware() if self._parent is not None else []
        return outer + self._middleware

    def _add(self, methods, path, handler):
        full = join_path(self._prefix, path)
        for method in methods:
            self._app._routes.append((method, full, handler, self))

    def _group_context(self):
        return self._app, self._prefix, self

    def __repr__(self):
        return f"<Group {self._prefix}>"
