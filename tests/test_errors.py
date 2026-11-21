import asyncio
import json
import os
import signal
from contextlib import asynccontextmanager

import pytest

from cyengine import App, Forbidden, HTTPError, NotFound, Response
from cyengine.middleware import recover

PORT = 8767


async def missing(request):
    raise NotFound("user not found")


async def teapot(request):
    raise HTTPError(418, {"why": "short and stout"}, headers={"X-Tea": "1"})


async def plain(request):
    raise Forbidden()


async def boom(request):
    raise RuntimeError("x")


@asynccontextmanager
async def running(with_recover: bool):
    app = App()
    if with_recover:
        app.use(recover)
    for path, h in (("/missing", missing), ("/teapot", teapot),
                    ("/plain", plain), ("/boom", boom)):
        app.handle("GET", path, h)
    task = asyncio.create_task(app.serve(host="127.0.0.1", port=PORT, grace=2))
    await asyncio.sleep(0.3)
    try:
        yield
    finally:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, 5)


async def get(path, accept=None):
    """Возвращает (status_line, headers, body); два запроса на одном соединении."""
    reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
    extra = f"Accept: {accept}\r\n" if accept else ""
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n{extra}\r\n".encode())
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), 3)
    # соединение должно остаться живым — ошибка хендлера не рвёт keep-alive
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n{extra}\r\n".encode())
    await writer.drain()
    second = await asyncio.wait_for(reader.read(65536), 3)
    writer.close()
    assert second.split(b"\r\n")[0] == data.split(b"\r\n")[0]
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    return lines[0], {k.lower(): v for k, v in (l.split(": ", 1) for l in lines[1:])}, body


@pytest.mark.parametrize("with_recover", [False, True])
def test_json_by_default(with_recover):
    async def main():
        async with running(with_recover):
            status, headers, body = await get("/missing")
            assert status == "HTTP/1.1 404 Not Found"
            assert headers["content-type"] == "application/json"
            assert json.loads(body) == {"error": "user not found"}
    asyncio.run(main())


@pytest.mark.parametrize("with_recover", [False, True])
def test_custom_status_detail_and_headers(with_recover):
    async def main():
        async with running(with_recover):
            status, headers, body = await get("/teapot")
            assert status == "HTTP/1.1 418 I'm a Teapot"
            assert headers["x-tea"] == "1"
            assert json.loads(body) == {"error": {"why": "short and stout"}}
    asyncio.run(main())


def test_text_when_client_asks_for_text():
    async def main():
        async with running(False):
            status, headers, body = await get("/missing", accept="text/plain")
            assert headers["content-type"].startswith("text/plain")
            assert body == b"user not found"
            # default detail = reason phrase
            _, _, body = await get("/plain", accept="text/plain")
            assert body == b"Forbidden"
            # клиент просит и то и другое → JSON
            _, headers, _ = await get("/missing", accept="text/plain, application/json")
            assert headers["content-type"] == "application/json"
    asyncio.run(main())


def test_other_exceptions_still_500():
    async def main():
        async with running(False):
            reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
            writer.write(b"GET /boom HTTP/1.1\r\nHost: x\r\n\r\n")
            assert (await reader.read(4096)).startswith(b"HTTP/1.1 500")
            writer.close()
    asyncio.run(main())


def test_status_must_be_error_code():
    with pytest.raises(ValueError):
        HTTPError(200)
    with pytest.raises(ValueError):
        HTTPError(302)
