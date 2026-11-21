import asyncio
import json
import os
import signal
from contextlib import asynccontextmanager

import pytest

from cyengine import App, HTTPError, NotFound, Response
from cyengine.handler import HandlerFunc

PORT = 8768


class DomainError(Exception): ...
class UserGone(DomainError): ...


async def raise_domain(request): raise DomainError("plain")
async def raise_gone(request): raise UserGone("gone")
async def raise_http(request): raise NotFound("nf")
async def raise_other(request): raise RuntimeError("x")
async def ok(request): return Response.text("ok")


def stamp(next):
    """middleware видит уже готовый Response от error handler'а."""
    async def handle(request):
        resp = await next.handle(request)
        resp.headers["X-Status-Seen"] = str(resp.status)
        return resp
    return HandlerFunc(handle)


@asynccontextmanager
async def running(configure):
    app = App()
    app.use(stamp)
    for path, h in (("/domain", raise_domain), ("/gone", raise_gone),
                    ("/http", raise_http), ("/other", raise_other), ("/ok", ok)):
        app.handle("GET", path, h)
    configure(app)
    task = asyncio.create_task(app.serve(host="127.0.0.1", port=PORT, grace=2))
    await asyncio.sleep(0.3)
    try:
        yield
    finally:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, 5)


async def get(path):
    reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), 3)
    writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    return int(lines[0].split()[1]), {k.lower(): v for k, v in (l.split(": ", 1) for l in lines[1:])}, body


def test_custom_handler_and_most_specific_wins():
    def configure(app):
        async def domain(request, exc):
            return Response.json({"kind": "domain", "msg": str(exc)}, status=400)

        async def gone(request, exc):
            return Response.json({"kind": "gone"}, status=410)

        app.error_handler(DomainError, domain)       # функция передана аргументом
        app.error_handler(UserGone)(gone)            # декоратор

    async def main():
        async with running(configure):
            status, headers, body = await get("/domain")
            assert status == 400 and json.loads(body) == {"kind": "domain", "msg": "plain"}
            assert headers["x-status-seen"] == "400"          # middleware видит ответ
            status, _, body = await get("/gone")               # подкласс → свой handler
            assert status == 410 and json.loads(body) == {"kind": "gone"}
            assert (await get("/ok"))[0] == 200
    asyncio.run(main())


def test_unregistered_exception_still_500_and_httperror_default_works():
    async def main():
        async with running(lambda app: None):
            assert (await get("/other"))[0] == 500
            status, _, body = await get("/http")
            assert status == 404 and json.loads(body) == {"error": "nf"}
    asyncio.run(main())


def test_override_httperror_and_catch_all():
    def configure(app):
        async def http(request, exc):
            return Response.text(f"custom {exc.status}", status=exc.status)

        async def anything(request, exc):
            return Response.json({"oops": type(exc).__name__}, status=500)

        app.error_handler(HTTPError, http)
        app.error_handler(Exception, anything)

    async def main():
        async with running(configure):
            status, _, body = await get("/http")
            assert status == 404 and body == b"custom 404"
            status, _, body = await get("/other")
            assert status == 500 and json.loads(body) == {"oops": "RuntimeError"}
    asyncio.run(main())


def test_registration_validation():
    app = App()

    async def fn(request, exc): ...
    with pytest.raises(TypeError):
        app.error_handler("nope", fn)
    with pytest.raises(TypeError):
        app.error_handler(int, fn)                    # не Exception
    with pytest.raises(TypeError):
        app.error_handler(DomainError, lambda r, e: None)   # не async
