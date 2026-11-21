"""Вход/выход через pydantic-core vs «обычный» путь через dict. PYTHONPATH=. python benchmarks/validation.py"""
import time
import tracemalloc

import orjson
from pydantic import BaseModel, Field

from cyengine import Response
from cyengine.core._types import Request


class Address(BaseModel):
    city: str
    zip: str


class User(BaseModel):
    id: int
    name: str
    email: str
    age: int = Field(ge=0)
    tags: list[str]
    address: Address


BODY = orjson.dumps({"id": 1, "name": "Ann", "email": "ann@example.com", "age": 30,
                     "tags": ["a", "b", "c"], "address": {"city": "Paris", "zip": "75001"}})
N = 200_000


def per_call(fn, n=N):
    fn()
    t = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t) / n * 1e6


def peak_bytes(fn):
    fn()
    tracemalloc.start()
    tracemalloc.reset_peak()
    base = tracemalloc.get_traced_memory()[0]
    for _ in range(100):
        fn()
    peak = tracemalloc.get_traced_memory()[1] - base
    tracemalloc.stop()
    return peak


r = Request()
r._body = BODY
user = User.model_validate_json(BODY)

cases = {
    "вход  request.parse(User)            (validate_json)": lambda: r.parse(User),
    "вход  User(**orjson.loads(body))     (через dict)":    lambda: User(**orjson.loads(BODY)),
    "выход Response.json(user)            (to_json)":       lambda: Response.json(user),
    "выход Response.json(user.model_dump()) (через dict)":  lambda: Response.json(user.model_dump()),
}
for name, fn in cases.items():
    print(f"{per_call(fn):6.2f} µs  {name}")

print()
print("пик памяти за 100 вызовов, байт (tracemalloc):")
print(f"  parse(User) {peak_bytes(lambda: r.parse(User))}   vs   User(**loads) {peak_bytes(lambda: User(**orjson.loads(BODY)))}")
print(f"  Response.json(user) {peak_bytes(lambda: Response.json(user))}   vs   Response.json(model_dump()) {peak_bytes(lambda: Response.json(user.model_dump()))}")
