import asyncio
import os
import signal

import pytest

from cyengine import App, Response
from cyengine.middleware import recover

PORT = 8765


async def ok(request):
    return Response.json({"id": request.path_params["id"]})


async def boom(request):
    raise RuntimeError("x")


async def bad(request):
    return {"not": "a Response"}


async def slow(request):
    await asyncio.sleep(0.3)
    return Response.text("slow-done")


async def fetch(raw: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
    writer.write(raw)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), 3)
    writer.close()
    return data


def get(path):
    return f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode()


def make_app():
    app = App()
    app.use(recover)
    app.handle("GET", "/users/<int:id>", ok)
    app.handle("GET", "/boom", boom)
    app.handle("GET", "/bad", bad)
    app.handle("GET", "/slow", slow)
    return app


async def started(app):
    task = asyncio.create_task(app.serve(host="127.0.0.1", port=PORT))
    await asyncio.sleep(0.3)
    return task


def test_requests_and_errors():
    async def main():
        task = await started(make_app())
        try:
            assert (await fetch(get("/users/7"))).endswith(b'{"id":7}')
            assert (await fetch(get("/nope"))).startswith(b"HTTP/1.1 404")
            assert (await fetch(get("/boom"))).startswith(b"HTTP/1.1 500")
            assert (await fetch(get("/bad"))).startswith(b"HTTP/1.1 500")
        finally:
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.wait_for(task, 5)

    asyncio.run(main())


def test_graceful_shutdown_finishes_inflight_and_returns():
    async def main():
        task = await started(make_app())
        reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
        writer.write(b"GET /slow HTTP/1.1\r\nHost: x\r\n\r\n")
        await asyncio.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)
        assert (await asyncio.wait_for(reader.read(4096), 3)).endswith(b"slow-done")
        await asyncio.wait_for(task, 5)  # serve() вернулся, keep-alive закрыт

    asyncio.run(main())


def test_sync_handler_rejected_at_registration():
    with pytest.raises(TypeError):
        App().handle("GET", "/s", lambda request: None)
