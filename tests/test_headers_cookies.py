import asyncio
import datetime
import os
import signal
from contextlib import asynccontextmanager

import pytest

from cyengine import App, Response
from cyengine.core._types import Request

PORT = 8774


def lines_of(raw: bytes):
    head = raw.partition(b"\r\n\r\n")[0].decode("latin-1")
    return head.split("\r\n")


def header_values(raw: bytes, name: str):
    prefix = name.lower() + ": "
    return [l[len(prefix):] for l in lines_of(raw)[1:] if l.lower().startswith(prefix)]


# ── header injection ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [
    "a\r\nSet-Cookie: evil=1",           # подмена заголовков
    "a\nX-Evil: 1",
    "a\r\n\r\n<script>",                 # подмена тела
    "a\x00b",
])
def test_crlf_and_nul_in_header_value_rejected(value):
    with pytest.raises(ValueError):
        Response(b"x", headers={"X-Test": value}).render()


@pytest.mark.parametrize("name", ["X Bad", "X:Bad", "X\r\nEvil", "", "X-é"])
def test_invalid_header_name_rejected(name):
    with pytest.raises(ValueError):
        Response(b"x", headers={name: "v"}).render()


def test_injection_in_list_value_rejected_and_clean_values_pass():
    with pytest.raises(ValueError):
        Response(b"x", headers={"Set-Cookie": ["a=1", "b=2\r\nX: y"]}).render()
    raw = Response(b"x", headers={"X-Name": "Zoë", "X-Plain": "ok"}).render()   # latin-1 допустим
    assert header_values(raw, "X-Plain") == ["ok"]


@asynccontextmanager
async def running():
    app = App()

    async def evil(request):               # значение пришло «от пользователя»
        return Response.text("hi", headers={"Location": request.query.get("to", "")})

    async def non_latin1(request):
        return Response.text("hi", headers={"X": "привет"})

    async def ok(request):
        return Response.text("ok")

    app.handle("GET", "/redirect", evil)
    app.handle("GET", "/cyrillic", non_latin1)
    app.handle("GET", "/ok", ok)
    task = asyncio.create_task(app.serve(host="127.0.0.1", port=PORT, grace=2))
    await asyncio.sleep(0.3)
    try:
        yield
    finally:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, 5)


async def raw_get(path):
    reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), 3)   # не повисает: ответ приходит
    writer.close()
    return data


def test_injection_through_handler_gives_500_not_hang_and_server_survives():
    async def main():
        async with running():
            # %0d%0a в query декодируется в CR LF → попытка внедрить Set-Cookie
            data = await raw_get("/redirect?to=/x%0d%0aSet-Cookie:%20evil=1")
            assert data.startswith(b"HTTP/1.1 500")
            assert b"evil" not in data
            # любая ошибка сборки ответа (не latin-1) — тоже 500, а не зависшее соединение
            assert (await raw_get("/cyrillic")).startswith(b"HTTP/1.1 500")
            assert (await raw_get("/ok")).startswith(b"HTTP/1.1 200")
    asyncio.run(main())


# ── несколько значений заголовка ─────────────────────────────────────────────

def test_list_value_renders_one_line_per_item():
    raw = Response(b"x", headers={"Set-Cookie": ["a=1", "b=2"], "X-One": "1"}).render()
    assert header_values(raw, "Set-Cookie") == ["a=1", "b=2"]
    assert header_values(raw, "X-One") == ["1"]


# ── set_cookie / delete_cookie ───────────────────────────────────────────────

def cookies_of(resp):
    return header_values(resp.render(), "Set-Cookie")


def test_set_cookie_defaults_are_safe():
    assert cookies_of(Response().set_cookie("sid", "abc")) == [
        "sid=abc; Path=/; HttpOnly; SameSite=Lax"]


def test_set_cookie_all_attributes():
    exp = datetime.datetime(2030, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
    c = cookies_of(Response().set_cookie(
        "sid", "abc", max_age=3600, expires=exp, path="/app", domain="example.com",
        secure=True, httponly=False, samesite="strict"))[0]
    assert c == ("sid=abc; Max-Age=3600; Expires=Wed, 02 Jan 2030 03:04:05 GMT; "
                 "Path=/app; Domain=example.com; Secure; SameSite=Strict")


def test_naive_expires_is_utc_and_samesite_none_needs_secure():
    c = cookies_of(Response().set_cookie("a", "1", expires=datetime.datetime(2030, 1, 1)))[0]
    assert "Expires=Tue, 01 Jan 2030 00:00:00 GMT" in c
    with pytest.raises(ValueError):
        Response().set_cookie("a", "1", samesite="none")
    assert "SameSite=None" in cookies_of(Response().set_cookie("a", "1", secure=True, samesite="none"))[0]
    assert "SameSite" not in cookies_of(Response().set_cookie("a", "1", samesite=None))[0]


def test_multiple_cookies_accumulate_and_chain():
    r = Response().set_cookie("a", "1").set_cookie("b", "2")
    assert [c.split(";")[0] for c in cookies_of(r)] == ["a=1", "b=2"]
    r2 = Response(headers={"Set-Cookie": "legacy=1"}).set_cookie("b", "2")   # уже была строка
    assert [c.split(";")[0] for c in cookies_of(r2)] == ["legacy=1", "b=2"]


@pytest.mark.parametrize("name,value", [
    ("bad name", "v"), ("a;b", "v"), ("n", "a b"), ("n", "a;b"), ("n", 'a"b'),
    ("n", "a,b"), ("n", "a\\b"), ("n", "é"), ("n", "a\r\nSet-Cookie: x=1"),
])
def test_set_cookie_rejects_unsafe_name_or_value(name, value):
    with pytest.raises(ValueError):
        Response().set_cookie(name, value)


def test_set_cookie_rejects_injection_via_attributes_and_bad_samesite():
    for kw in ({"path": "/; Secure"}, {"domain": "a.com\r\nX: y"}, {"samesite": "bogus"}):
        with pytest.raises(ValueError):
            Response().set_cookie("a", "1", **kw)


def test_delete_cookie():
    c = cookies_of(Response().delete_cookie("sid", path="/app", domain="example.com"))[0]
    assert c.startswith("sid=; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/app; Domain=example.com")


# ── request.cookies ──────────────────────────────────────────────────────────

def req(cookie=None):
    r = Request()
    if cookie is not None:
        r.headers["cookie"] = cookie
    return r


def test_request_cookies_parsing():
    assert req().cookies == {}
    assert req("a=1; b=2").cookies == {"a": "1", "b": "2"}
    assert req('a=1;b="x y"; c = 3').cookies == {"a": "1", "b": "x y", "c": "3"}
    assert req("a=1; a=2").cookies == {"a": "1"}                  # первый побеждает
    assert req("junk; =x; ok=1; ;").cookies == {"ok": "1"}         # мусор пропускается
    assert req("a=b=c").cookies == {"a": "b=c"}                    # '=' в значении
    r = req("a=1")
    assert r.cookies is r.cookies                                   # парсится один раз
