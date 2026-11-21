import asyncio
import logging
import os
import signal
from contextlib import asynccontextmanager

import pytest

from cyengine import App, Response
from cyengine.middleware import access_log, cors, recover

PORT = 8771
CALLS = []


async def hello(request):
    CALLS.append(request.path)
    return Response.json({"hi": 1})


async def vary_handler(request):
    return Response.text("v", headers={"vary": "Accept-Encoding"})


async def boom(request):
    raise RuntimeError("x")


@asynccontextmanager
async def running(*middleware):
    CALLS.clear()
    app = App()
    app.use(*middleware)
    app.handle("GET", "/hello", hello)
    app.handle("POST", "/hello", hello)
    app.handle("GET", "/vary", vary_handler)
    app.handle("GET", "/boom", boom)
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


async def call(method, path, **headers):
    extra = "".join(f"{k.replace('_', '-')}: {v}\r\n" for k, v in headers.items())
    reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
    writer.write(f"{method} {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n{extra}\r\n".encode())
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), 3)
    writer.close()
    return parse(data)


# ── access log ───────────────────────────────────────────────────────────────

def test_access_log_lines(caplog):
    caplog.set_level(logging.INFO, logger="cyengine.access")

    async def main():
        async with running(access_log()):
            await call("GET", "/hello?x=1&y=%20")
            await call("GET", "/missing")
            await call("GET", "/boom")
    asyncio.run(main())

    lines = [r.getMessage() for r in caplog.records if r.name == "cyengine.access"]
    assert len(lines) == 3
    assert lines[0].startswith('127.0.0.1 "GET /hello?x=1&y=%20" 200 ')
    assert lines[0].endswith("ms") and "b " in lines[0]
    assert '"GET /missing" 404' in lines[1]            # 404 тоже в логе
    assert '"GET /boom" 500' in lines[2]               # необработанное исключение → 500


def test_access_log_passes_exception_through():
    async def main():
        async with running(access_log()):
            status, _, _ = await call("GET", "/boom")
            assert status == 500                       # access_log не глотает исключение
    asyncio.run(main())


# ── CORS ─────────────────────────────────────────────────────────────────────

def test_cors_wildcard_default():
    async def main():
        async with running(cors()):
            _, h, _ = await call("GET", "/hello", Origin="https://a.com")
            assert h["access-control-allow-origin"] == "*"
            assert "vary" not in h                      # «*» не зависит от Origin
            _, h, _ = await call("GET", "/hello")       # не CORS-запрос
            assert "access-control-allow-origin" not in h
    asyncio.run(main())


def test_cors_specific_origins_and_vary():
    async def main():
        async with running(cors(["https://a.com"], expose_headers=["X-Id"])):
            _, h, _ = await call("GET", "/hello", Origin="https://a.com")
            assert h["access-control-allow-origin"] == "https://a.com"
            assert h["access-control-expose-headers"] == "X-Id"
            assert h["vary"] == "Origin"
            _, h, _ = await call("GET", "/hello", Origin="https://evil.com")
            assert "access-control-allow-origin" not in h and h["vary"] == "Origin"
            _, h, _ = await call("GET", "/hello")       # без Origin: Vary всё равно нужен
            assert h["vary"] == "Origin"
    asyncio.run(main())


def test_cors_regex_and_credentials_reflect_origin():
    async def main():
        async with running(cors(allow_origin_regex=r"https://.*\.example\.com",
                                allow_credentials=True)):
            _, h, _ = await call("GET", "/hello", Origin="https://app.example.com")
            assert h["access-control-allow-origin"] == "https://app.example.com"
            assert h["access-control-allow-credentials"] == "true"
            _, h, _ = await call("GET", "/hello", Origin="https://example.com.evil.io")
            assert "access-control-allow-origin" not in h
    asyncio.run(main())


def test_cors_wildcard_with_credentials_echoes_origin_not_star():
    async def main():
        async with running(cors(allow_credentials=True)):
            _, h, _ = await call("GET", "/hello", Origin="https://a.com")
            assert h["access-control-allow-origin"] == "https://a.com"
            assert h["vary"] == "Origin"
    asyncio.run(main())


def test_cors_preflight_answered_without_handler():
    async def main():
        async with running(cors(["https://a.com"], max_age=99)):
            status, h, body = await call(
                "OPTIONS", "/hello", Origin="https://a.com",
                Access_Control_Request_Method="POST",
                Access_Control_Request_Headers="content-type, x-token")
            assert status == 204 and body == b""
            assert h["access-control-allow-origin"] == "https://a.com"
            assert "POST" in h["access-control-allow-methods"]
            assert h["access-control-allow-headers"] == "content-type, x-token"   # эхо
            assert h["access-control-max-age"] == "99"
            assert "Origin" in h["vary"] and "Access-Control-Request-Method" in h["vary"]
            assert CALLS == []                                   # хендлер не вызывался
    asyncio.run(main())


def test_cors_preflight_fixed_allow_headers():
    async def main():
        async with running(cors(allow_headers=["Authorization"])):
            _, h, _ = await call("OPTIONS", "/hello", Origin="https://a.com",
                                 Access_Control_Request_Method="GET",
                                 Access_Control_Request_Headers="x-other")
            assert h["access-control-allow-headers"] == "Authorization"
    asyncio.run(main())


def test_cors_preflight_disallowed_origin_403():
    async def main():
        async with running(cors(["https://a.com"])):
            status, h, _ = await call("OPTIONS", "/hello", Origin="https://evil.com",
                                      Access_Control_Request_Method="POST")
            assert status == 403 and "access-control-allow-origin" not in h
    asyncio.run(main())


def test_plain_options_without_acrm_goes_to_router():
    async def main():
        async with running(cors(["https://a.com"])):
            status, h, _ = await call("OPTIONS", "/hello", Origin="https://a.com")
            assert status == 204 and "GET" in h["allow"]         # автоответ OPTIONS
            assert h["access-control-allow-origin"] == "https://a.com"
    asyncio.run(main())


def test_cors_headers_on_errors_and_vary_merge():
    async def main():
        async with running(cors(["https://a.com"]), recover):
            _, h, _ = await call("GET", "/missing", Origin="https://a.com")
            assert h["access-control-allow-origin"] == "https://a.com"      # на 404
            _, h, _ = await call("GET", "/boom", Origin="https://a.com")
            assert h["access-control-allow-origin"] == "https://a.com"      # на 500 после recover
            _, h, _ = await call("GET", "/vary", Origin="https://a.com")
            assert h["vary"] == "Accept-Encoding, Origin"                   # без дубля, без затирания
    asyncio.run(main())


def test_middleware_order_combo():
    async def main():
        async with running(access_log(), cors(), recover):
            status, h, _ = await call("GET", "/hello", Origin="https://a.com")
            assert status == 200 and h["access-control-allow-origin"] == "*"
    asyncio.run(main())
