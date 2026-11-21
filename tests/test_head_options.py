import asyncio
import os
import signal
from contextlib import asynccontextmanager

from cyengine import App, Response
from cyengine.handler import HandlerFunc

PORT = 8770
BODY = b'{"hello":"world"}'


def mark(next):
    async def handle(request):
        resp = await next.handle(request)
        resp.headers["X-Method-Seen"] = request.method
        return resp
    return HandlerFunc(handle)


async def items(request):
    return Response.json({"hello": "world"})


async def create(request):
    return Response.text("created", status=201)


async def head_own(request):
    return Response.text("own", headers={"X-Own": "1"})


async def get_only_sibling(request):
    return Response.text("sibling")


async def opts_own(request):
    return Response.text("custom options", headers={"Allow": "GET"})


@asynccontextmanager
async def running():
    app = App()
    app.use(mark)
    app.handle("GET", "/items", items)
    app.handle("POST", "/items", create)
    app.handle("GET", "/own", get_only_sibling)
    app.handle("HEAD", "/own", head_own)          # свой HEAD приоритетнее
    app.handle("GET", "/custom", items)
    app.handle("OPTIONS", "/custom", opts_own)    # свой OPTIONS приоритетнее
    app.handle("POST", "/post-only", create)
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


def test_head_mirrors_get_without_body():
    async def main():
        async with running():
            g_status, g_headers, g_body = await call("GET", "/items")
            h_status, h_headers, h_body = await call("HEAD", "/items")
            assert g_body == BODY and h_body == b""
            assert h_status == g_status == 200
            assert h_headers["content-length"] == g_headers["content-length"] == str(len(BODY))
            assert h_headers["content-type"] == g_headers["content-type"]
            assert h_headers["x-method-seen"] == "HEAD"        # middleware видит HEAD
    asyncio.run(main())


def test_head_does_not_corrupt_keepalive_stream():
    async def main():
        async with running():
            reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
            writer.write(b"HEAD /items HTTP/1.1\r\nHost: x\r\n\r\n")
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
            assert head.startswith(b"HTTP/1.1 200")
            # тела не было: следующий ответ должен начинаться чисто, а не с хвоста HEAD
            writer.write(b"GET /items HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            await writer.drain()
            status, _, body = parse(await asyncio.wait_for(reader.read(65536), 3))
            assert status == 200 and body == BODY
            writer.close()
    asyncio.run(main())


def test_explicit_head_handler_wins():
    async def main():
        async with running():
            status, headers, body = await call("HEAD", "/own")
            assert status == 200 and headers["x-own"] == "1" and body == b""
    asyncio.run(main())


def test_head_on_missing_and_on_post_only():
    async def main():
        async with running():
            status, headers, body = await call("HEAD", "/missing")
            assert status == 404 and body == b"" and int(headers["content-length"]) > 0
            status, headers, _ = await call("HEAD", "/post-only")
            assert status == 405 and headers["allow"] == "OPTIONS, POST"
    asyncio.run(main())


def test_auto_options():
    async def main():
        async with running():
            status, headers, body = await call("OPTIONS", "/items")
            assert status == 204 and body == b""
            assert headers["allow"] == "GET, HEAD, OPTIONS, POST"
            assert "content-length" not in headers                 # у 204 его быть не должно
            assert headers["x-method-seen"] == "OPTIONS"           # через middleware
            assert (await call("OPTIONS", "/missing"))[0] == 404
    asyncio.run(main())


def test_explicit_options_handler_wins():
    async def main():
        async with running():
            status, headers, body = await call("OPTIONS", "/custom")
            assert status == 200 and body == b"custom options" and headers["allow"] == "GET"
    asyncio.run(main())
