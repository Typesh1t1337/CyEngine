import asyncio
import json
import os
import signal
from contextlib import asynccontextmanager

import pytest

from cyengine import App, NotFound, Response
from cyengine.handler import HandlerFunc
from cyengine.routing import join_path

PORT = 8772


def tag(name):
    """Middleware: дописывает имя в request.state['trace'] и метит ответ."""
    def middleware(next):
        async def handle(request):
            request.state.setdefault("trace", []).append(name)
            resp = await next.handle(request)
            resp.headers.setdefault("X-Seen", "")
            resp.headers["X-Seen"] += name + ";"      # порядок выхода: изнутри наружу
            return resp
        return HandlerFunc(handle)
    return middleware


async def trace(request):
    return Response.json({"trace": request.state.get("trace", []),
                          "params": request.path_params})


async def missing(request):
    raise NotFound("gone")


@asynccontextmanager
async def running(configure):
    app = App()
    configure(app)
    task = asyncio.create_task(app.serve(host="127.0.0.1", port=PORT, grace=2))
    await asyncio.sleep(0.3)
    try:
        yield
    finally:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, 5)


def parse(data: bytes):
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    return (int(lines[0].split()[1]),
            {k.lower(): v for k, v in (l.split(": ", 1) for l in lines[1:])}, body)


async def call(method, path):
    reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
    writer.write(f"{method} {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), 3)
    writer.close()
    return parse(data)


def test_join_path():
    assert join_path("/api/", "/users/") == "/api/users"
    assert join_path("/api", "/") == "/api"
    assert join_path("/api", "") == "/api"
    assert join_path("", "/x") == "/x"
    assert join_path("/", "/") == "/"
    assert join_path("/a", "b/c") == "/a/b/c"


def test_prefix_and_nested_groups():
    def configure(app):
        api = app.group("/api")
        api.handle("GET", "/users/<int:id>", trace)
        api.handle("GET", "/", trace)                 # → /api
        v1 = api.group("/v1/")
        v1.handle("GET", "/ping", trace)              # → /api/v1/ping
        app.handle("GET", "/plain", trace)

    async def main():
        async with running(configure):
            status, _, body = await call("GET", "/api/users/7")
            assert status == 200 and json.loads(body)["params"] == {"id": 7}
            assert (await call("GET", "/api"))[0] == 200
            assert (await call("GET", "/api/v1/ping"))[0] == 200
            assert (await call("GET", "/plain"))[0] == 200
            assert (await call("GET", "/users/7"))[0] == 404          # без префикса — нет
    asyncio.run(main())


def test_param_in_prefix():
    def configure(app):
        users = app.group("/users/<int:uid>")
        users.handle("GET", "/posts/<int:pid>", trace)

    async def main():
        async with running(configure):
            _, _, body = await call("GET", "/users/3/posts/9")
            assert json.loads(body)["params"] == {"uid": 3, "pid": 9}
    asyncio.run(main())


def test_middleware_order_and_scope():
    def configure(app):
        app.use(tag("app"))
        api = app.group("/api", tag("api"))
        v1 = api.group("/v1", tag("v1"))
        v1.handle("GET", "/x", trace)
        api.handle("GET", "/y", trace)
        app.handle("GET", "/z", trace)

    async def main():
        async with running(configure):
            _, h, body = await call("GET", "/api/v1/x")
            assert json.loads(body)["trace"] == ["app", "api", "v1"]    # снаружи внутрь
            assert h["x-seen"] == "v1;api;app;"                         # и обратно
            _, h, body = await call("GET", "/api/y")
            assert json.loads(body)["trace"] == ["app", "api"]          # v1 сюда не относится
            _, _, body = await call("GET", "/z")
            assert json.loads(body)["trace"] == ["app"]                 # вне группы — только app
    asyncio.run(main())


def test_group_use_after_registering_routes_still_applies():
    def configure(app):
        api = app.group("/api")
        api.handle("GET", "/x", trace)
        api.use(tag("late"))                    # после handle — всё равно действует

    async def main():
        async with running(configure):
            _, _, body = await call("GET", "/api/x")
            assert json.loads(body)["trace"] == ["late"]
    asyncio.run(main())


def test_decorators_on_group():
    def configure(app):
        api = app.group("/api")

        @api.get("/hi")
        async def hi(request):
            return Response.text("hi")

        @api.route("/both", ["GET", "POST"])
        async def both(request):
            return Response.text(request.method)

    async def main():
        async with running(configure):
            assert (await call("GET", "/api/hi"))[2] == b"hi"
            assert (await call("POST", "/api/both"))[2] == b"POST"
            status, h, _ = await call("DELETE", "/api/both")
            assert status == 405 and h["allow"] == "GET, HEAD, OPTIONS, POST"
    asyncio.run(main())


def test_group_middleware_sees_errors_and_error_handlers_apply():
    def configure(app):
        async def custom(request, exc):
            return Response.json({"custom": exc.detail}, status=404)
        app.error_handler(NotFound, custom)
        api = app.group("/api", tag("api"))
        api.handle("GET", "/gone", missing)

    async def main():
        async with running(configure):
            status, h, body = await call("GET", "/api/gone")
            assert status == 404 and json.loads(body) == {"custom": "gone"}
            assert h["x-seen"] == "api;"        # middleware группы видит готовый ответ
    asyncio.run(main())


def test_auto_options_goes_through_group_middleware():
    def configure(app):
        app.use(tag("app"))
        api = app.group("/api", tag("api"))
        api.handle("GET", "/x", trace)
        app.handle("GET", "/z", trace)

    async def main():
        async with running(configure):
            status, h, _ = await call("OPTIONS", "/api/x")
            assert status == 204 and h["x-seen"] == "api;app;"          # цепочка группы
            status, h, _ = await call("OPTIONS", "/z")
            assert status == 204 and h["x-seen"] == "app;"
    asyncio.run(main())


def test_404_in_prefix_passes_only_app_middleware():
    def configure(app):
        app.use(tag("app"))
        api = app.group("/api", tag("api"))
        api.handle("GET", "/x", trace)

    async def main():
        async with running(configure):
            status, h, _ = await call("GET", "/api/nope")
            assert status == 404 and h["x-seen"] == "app;"
    asyncio.run(main())


def test_invalid_prefix_rejected():
    app = App()
    with pytest.raises(ValueError):
        app.group("api")
    with pytest.raises(ValueError):
        app.group("/api").group("v1")
    app.group("")          # без префикса и "/" допустимы
    app.group("/")
