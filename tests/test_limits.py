import asyncio
import os
import signal
from contextlib import asynccontextmanager

from cyengine import App, Limits, Response

PORT = 8766


async def echo_len(request):
    return Response.text(str(len(request.body())))


async def slow(request):
    await asyncio.sleep(0.6)
    return Response.text("slow-done")


@asynccontextmanager
async def running(**limits):
    app = App(limits=Limits(**limits))
    app.handle("POST", "/echo", echo_len)
    app.handle("GET", "/slow", slow)
    task = asyncio.create_task(app.serve(host="127.0.0.1", port=PORT, grace=2))
    await asyncio.sleep(0.3)
    try:
        yield
    finally:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, 5)


async def exchange(raw: bytes, read_timeout=3) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
    writer.write(raw)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), read_timeout)
    writer.close()
    return data


def post(body: bytes, extra=b""):
    return (b"POST /echo HTTP/1.1\r\nHost: x\r\nConnection: close\r\n" + extra
            + b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)


def test_body_within_limit_ok():
    async def main():
        async with running(max_body_size=100):
            assert (await exchange(post(b"x" * 100))).endswith(b"100")
    asyncio.run(main())


def test_body_over_limit_413_by_content_length():
    async def main():
        async with running(max_body_size=100):
            # тело не дочитывается: отказ приходит сразу по заголовку
            raw = (b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 101\r\n\r\n")
            assert (await exchange(raw)).startswith(b"HTTP/1.1 413")
    asyncio.run(main())


def test_chunked_body_over_limit_413():
    async def main():
        async with running(max_body_size=100):
            chunk = b"x" * 60
            part = hex(len(chunk))[2:].encode() + b"\r\n" + chunk + b"\r\n"
            raw = (b"POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
                   + part * 2 + b"0\r\n\r\n")
            assert (await exchange(raw)).startswith(b"HTTP/1.1 413")
    asyncio.run(main())


def test_header_over_limit_431():
    async def main():
        async with running(max_header_size=512):
            raw = b"GET /slow HTTP/1.1\r\nHost: x\r\nX-Big: " + b"a" * 1000 + b"\r\n\r\n"
            assert (await exchange(raw)).startswith(b"HTTP/1.1 431")
    asyncio.run(main())


def test_unterminated_header_431():
    async def main():
        async with running(max_header_size=512):
            raw = b"GET /slow HTTP/1.1\r\nX-Big: " + b"a" * 2000   # без CRLF
            assert (await exchange(raw)).startswith(b"HTTP/1.1 431")
    asyncio.run(main())


def test_slowloris_408():
    async def main():
        async with running(read_timeout=0.3):
            reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
            writer.write(b"GET /slow HTTP/1.1\r\nHost: x\r\n")   # заголовки не закончены
            await writer.drain()
            data = await asyncio.wait_for(reader.read(65536), 3)
            assert data.startswith(b"HTTP/1.1 408")
            writer.close()
    asyncio.run(main())


def test_idle_keepalive_closed_silently():
    async def main():
        async with running(keepalive_timeout=0.3):
            reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
            assert await asyncio.wait_for(reader.read(65536), 3) == b""   # EOF
            writer.close()
    asyncio.run(main())


def test_handler_timeout_503():
    async def main():
        async with running(handler_timeout=0.2):
            raw = b"GET /slow HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
            assert (await exchange(raw)).startswith(b"HTTP/1.1 503")
    asyncio.run(main())


def test_keepalive_timer_does_not_kill_running_handler():
    async def main():
        async with running(keepalive_timeout=0.2, handler_timeout=5):
            raw = b"GET /slow HTTP/1.1\r\nHost: x\r\n\r\n"
            assert (await exchange(raw)).endswith(b"slow-done")
    asyncio.run(main())
