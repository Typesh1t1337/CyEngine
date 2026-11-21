import asyncio
import datetime
import json
import os
import signal
from contextlib import asynccontextmanager

import pytest
from pydantic import BaseModel, Field

from cyengine import App, Response
from cyengine.core._types import Request
from cyengine.errors import RequestValidationError

PORT = 8773


class CreateUser(BaseModel):
    name: str
    age: int = Field(ge=0)


class Profile(BaseModel):
    user_id: int = Field(alias="userId")
    created: datetime.date


class Pagination(BaseModel):
    page: int = 1
    limit: int = Field(10, le=100)


def req(body=b"", query=b""):
    r = Request()
    r._body = body
    r.query_string = query
    return r


# ── вход ─────────────────────────────────────────────────────────────────────

def test_parse_body_into_model():
    user = req(b'{"name": "Ann", "age": 30}').parse(CreateUser)
    assert isinstance(user, CreateUser) and user.name == "Ann" and user.age == 30


def test_parse_accepts_bytearray_body_and_generic_types():
    assert req(bytearray(b'[{"name":"a","age":1}]')).parse(list[CreateUser])[0].age == 1
    assert req(b'{"a": 1}').parse(dict[str, int]) == {"a": 1}


def test_parse_invalid_gives_422_with_source_in_loc():
    with pytest.raises(RequestValidationError) as e:
        req(b'{"name": "Ann", "age": -5}').parse(CreateUser)
    assert e.value.status == 422
    err = e.value.errors[0]
    assert err["loc"] == ["body", "age"] and err["type"] == "greater_than_equal"
    assert err["input"] == -5 and "msg" in err


def test_parse_missing_field_and_broken_json():
    with pytest.raises(RequestValidationError) as e:
        req(b'{"age": 1}').parse(CreateUser)
    assert e.value.errors[0]["loc"] == ["body", "name"] and e.value.errors[0]["type"] == "missing"
    for bad in (b"{nope", b""):
        with pytest.raises(RequestValidationError) as e:
            req(bad).parse(CreateUser)
        assert e.value.errors[0]["type"] == "json_invalid"


def test_parse_query():
    q = req(query=b"page=2&limit=5").parse_query(Pagination)
    assert (q.page, q.limit) == (2, 5)
    assert req().parse_query(Pagination).limit == 10                      # дефолты
    with pytest.raises(RequestValidationError) as e:
        req(query=b"limit=500").parse_query(Pagination)
    assert e.value.errors[0]["loc"] == ["query", "limit"]


def test_body_is_not_copied_for_single_chunk():
    body = b'{"name": "Ann", "age": 1}'
    r = req(body)
    assert r.body() is body and r._body is body


# ── выход ────────────────────────────────────────────────────────────────────

def test_response_json_model_uses_aliases_and_native_types():
    p = Profile(userId=7, created=datetime.date(2026, 1, 2))
    assert json.loads(Response.json(p).body) == {"userId": 7, "created": "2026-01-02"}
    assert Response.json(p).headers["Content-Type"] == "application/json"


def test_response_json_containers_with_models_and_plain_data():
    p = Profile(userId=1, created=datetime.date(2026, 1, 2))
    assert json.loads(Response.json([p, p]).body)[1]["userId"] == 1          # list[Model]
    assert json.loads(Response.json({"profile": p, "n": 1}).body)["profile"]["userId"] == 1
    assert Response.json({"a": 1, "b": [1, 2]}).body == b'{"a":1,"b":[1,2]}'  # без pydantic
    assert Response.json("text").body == b'"text"'


# ── через сервер ─────────────────────────────────────────────────────────────

async def create(request):
    user = request.parse(CreateUser)
    return Response.json(user, status=201)


@asynccontextmanager
async def running():
    app = App()
    app.handle("POST", "/users", create)
    task = asyncio.create_task(app.serve(host="127.0.0.1", port=PORT, grace=2))
    await asyncio.sleep(0.3)
    try:
        yield
    finally:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, 5)


def parse_http(data: bytes):
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), body


async def post(chunks, accept=None):
    reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
    total = sum(len(c) for c in chunks[1:])
    extra = f"Accept: {accept}\r\n" if accept else ""
    writer.write(f"POST /users HTTP/1.1\r\nHost: x\r\nConnection: close\r\n{extra}"
                 f"Content-Length: {total}\r\n\r\n".encode())
    for c in chunks[1:]:
        writer.write(c)
        await writer.drain()
        await asyncio.sleep(0.05)         # разные TCP-сегменты → несколько on_body
    data = await asyncio.wait_for(reader.read(65536), 3)
    writer.close()
    return parse_http(data)


def test_end_to_end_valid_invalid_and_chunked_body():
    async def main():
        async with running():
            status, body = await post([None, b'{"name": "Ann", "age": 30}'])
            assert status == 201 and json.loads(body) == {"name": "Ann", "age": 30}

            # тело двумя кусками (путь bytes → bytearray) даёт тот же результат
            status, body = await post([None, b'{"name": "An', b'n", "age": 30}'])
            assert status == 201 and json.loads(body)["name"] == "Ann"

            status, body = await post([None, b'{"name": "Ann", "age": -1}'])
            assert status == 422
            detail = json.loads(body)["error"]
            assert detail[0]["loc"] == ["body", "age"]

            status, body = await post([None, b"{broken"])
            assert status == 422 and json.loads(body)["error"][0]["type"] == "json_invalid"
    asyncio.run(main())
