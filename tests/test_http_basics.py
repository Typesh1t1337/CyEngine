import asyncio
import email.utils
import json
import os
import signal
import time
from contextlib import asynccontextmanager

from cyengine import App, NotFound, Response
from cyengine.core._router import Router
from cyengine.handler import HandlerFunc

PORT = 8769


def auth(next):
    async def handle(request):
        request.state["user"] = "bob"
        resp = await next.handle(request)
        resp.headers["X-Status-Seen"] = str(resp.status)
        return resp
    return HandlerFunc(handle)


async def whoami(request):
    return Response.text(request.state["user"])


async def echo_json(request):
    return Response.json(request.json())


async def file(request):
    return Response.text(request.path_params["name"])


async def query(request):
    return Response.json(request.query)


async def create(request):
    return Response.text("created", status=201)


@asynccontextmanager
async def running(configure=lambda app: None, **limits):
    app = App()
    app.use(auth)
    app.handle("GET", "/me", whoami)
    app.handle("POST", "/json", echo_json)
    app.handle("GET", "/files/<name>", file)
    app.handle("GET", "/q", query)
    app.handle("GET", "/res", create)
    app.handle("POST", "/res", create)
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


async def request(raw: bytes):
    reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
    writer.write(raw)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), 3)
    writer.close()
    return parse(data)


def req(method, path, body=b"", extra=""):
    return (f"{method} {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n{extra}"
            f"Content-Length: {len(body)}\r\n\r\n").encode() + body


# ── request.state ────────────────────────────────────────────────────────────

def test_state_passes_from_middleware_to_handler():
    async def main():
        async with running():
            status, _, body = await request(req("GET", "/me"))
            assert status == 200 and body == b"bob"
    asyncio.run(main())


# ── json() → 400 ─────────────────────────────────────────────────────────────

def test_invalid_and_empty_json_is_400():
    async def main():
        async with running():
            ok = await request(req("POST", "/json", b'{"a": 1}'))
            assert ok[0] == 200 and json.loads(ok[2]) == {"a": 1}
            for bad in (b"{nope", b""):
                status, _, body = await request(req("POST", "/json", bad))
                assert status == 400
                assert json.loads(body) == {"error": "invalid JSON body"}
    asyncio.run(main())


# ── декодирование URL ────────────────────────────────────────────────────────

def test_path_and_query_decoding():
    async def main():
        async with running():
            _, _, body = await request(req("GET", "/files/hello%20world"))
            assert body == b"hello world"
            # %2F внутри сегмента — не разделитель пути
            status, _, body = await request(req("GET", "/files/a%2Fb"))
            assert status == 200 and body == b"a/b"
            _, _, body = await request(req("GET", "/q?q=a%20b+c&x=%D0%B0&flag"))
            assert json.loads(body) == {"q": "a b c", "x": "а", "flag": ""}
    asyncio.run(main())


def test_router_decodes_after_split_and_lists_allowed():
    async def h(request): ...
    r = Router()
    r.add("GET", "/res", h)
    r.add("POST", "/res", h)
    r.add("GET", "/f/<name>", h)
    assert r.match("GET", "/f/a%2Fb")[1] == {"name": "a/b"}
    assert r.allowed("/res") == ["GET", "HEAD", "POST"]
    assert r.allowed("/nope") == []


# ── Date ─────────────────────────────────────────────────────────────────────

def test_date_header_everywhere():
    async def main():
        async with running():
            for raw in (req("GET", "/me"),                       # обычный ответ
                        req("GET", "/missing"),                  # 404 из цепочки
                        req("POST", "/files/x", b"y" * 5),       # 405
                        b"GET /me HTTP/1.1\r\nContent-Length: 99999999\r\n\r\n"):  # 413, сырой
                _, headers, _ = await request(raw)
                parsed = email.utils.parsedate_to_datetime(headers["date"])
                assert abs(parsed.timestamp() - time.time()) < 5
                assert headers["date"].endswith("GMT")
    asyncio.run(main())


# ── 404 / 405 через цепочку ──────────────────────────────────────────────────

def test_405_has_allow_and_json_body():
    async def main():
        async with running():
            status, headers, body = await request(req("DELETE", "/res"))
            assert status == 405
            assert headers["allow"] == "GET, HEAD, OPTIONS, POST"
            assert json.loads(body) == {"error": "Method Not Allowed"}
    asyncio.run(main())


def test_404_goes_through_middleware_and_error_handlers():
    def configure(app):
        async def custom(request, exc):
            return Response.json({"nope": request.path}, status=404)
        app.error_handler(NotFound, custom)

    async def main():
        async with running(configure):
            status, headers, body = await request(req("GET", "/missing"))
            assert status == 404 and json.loads(body) == {"nope": "/missing"}
            assert headers["x-status-seen"] == "404"        # middleware увидел 404
    asyncio.run(main())


def test_404_keeps_connection_alive():
    async def main():
        async with running():
            reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
            for _ in range(2):
                writer.write(b"GET /missing HTTP/1.1\r\nHost: x\r\n\r\n")
                await writer.drain()
                assert (await asyncio.wait_for(reader.read(65536), 3)).startswith(b"HTTP/1.1 404")
            writer.close()
    asyncio.run(main())
