"""
Стоимость стадий горячего пути в µs/запрос, без сети. Запуск (из корня):
    PYTHONPATH=. python benchmarks/stages.py
"""
import asyncio
import time

import httptools
import uvloop

from cyengine import App, Response, Limits
from cyengine.core._protocol import HttpProtocol
from cyengine.core._router import Router
from cyengine.core._types import Request
from cyengine.middleware import recover

REQ = (b"GET /users/42 HTTP/1.1\r\nHost: 127.0.0.1:8000\r\nUser-Agent: bombardier\r\n"
       b"Accept: */*\r\n\r\n")
PAYLOAD = {"message": "hello", "ok": True, "items": [1, 2, 3]}
N = 200_000


class FakeTransport:
    def __init__(self):
        self.writes = 0

    def write(self, data):
        self.writes += 1

    def close(self):
        pass

    def get_extra_info(self, *a, **k):
        return None


def per_call(fn, n=N):
    fn()
    t = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t) / n * 1e6


async def main():
    loop = asyncio.get_running_loop()

    async def user(request):
        return Response.json({"id": request.path_params["id"]})

    app = App()
    app.use(recover)
    app.handle("GET", "/users/<int:id>", user)
    app._router = app._build_router()
    router = app._router
    handler, params = router.match("GET", "/users/42")

    results = {}

    # ── полный цикл пачками по B соединений на итерацию loop (как под нагрузкой) ─
    B = 64

    async def full(n):
        protos = []
        trs = []
        for _ in range(B):
            p = app._make_protocol(loop)
            tr = FakeTransport()
            p.connection_made(tr)
            protos.append(p)
            trs.append(tr)
        rounds = n // B
        t = time.perf_counter()
        for _ in range(rounds):
            for p in protos:
                p.data_received(REQ)
            await asyncio.sleep(0)
        dt = time.perf_counter() - t
        assert sum(tr.writes for tr in trs) == rounds * B
        return dt / (rounds * B) * 1e6

    await full(6400)
    results["FULL cycle (data_received→write)"] = await full(N)

    # ── пол asyncio: create_task + пустая корутина, пачками ──────────────────
    async def noop():
        pass

    async def floor(n):
        rounds = n // B
        t = time.perf_counter()
        for _ in range(rounds):
            for _ in range(B):
                loop.create_task(noop())
            await asyncio.sleep(0)
        return (time.perf_counter() - t) / (rounds * B) * 1e6

    await floor(6400)
    results["  asyncio floor (create_task, noop)"] = await floor(N)

    # ── парсинг: наш протокол vs пустой httptools ────────────────────────────
    class Dummy:
        def on_url(self, u): pass
        def on_header(self, n, v): pass
        def on_headers_complete(self): pass
        def on_message_complete(self): pass

    d = Dummy()
    results["  httptools floor (empty callbacks)"] = per_call(
        lambda: httptools.HttpRequestParser(d).feed_data(REQ))

    class P(HttpProtocol):                      # без create_task на message_complete
        def on_message_complete(self):
            pass

    p = P(router, app, loop)
    p.connection_made(FakeTransport())
    results["  parse via our callbacks"] = per_call(lambda: (p._reset(), p._parser.feed_data(REQ)))

    results["  Request()"] = per_call(Request)
    results["  router.match (typed param)"] = per_call(lambda: router.match("GET", "/users/42"))
    results["  Response.json({id})"] = per_call(lambda: Response.json({"id": 42}))
    resp = Response.json({"id": 42})
    results["  response.render()"] = per_call(lambda: resp.render(True, False))

    # ── хендлер через middleware+error-wrap (без создания задач) ─────────────
    req = Request()
    req.path_params = params

    async def chain_cost(n):
        t = time.perf_counter()
        for _ in range(n):
            await handler(req)
        return (time.perf_counter() - t) / n * 1e6

    await chain_cost(5000)
    results["  handler chain (recover+errwrap+fn)"] = await chain_cost(N)

    for k, v in results.items():
        print(f"{v:8.2f} µs  {k}")


uvloop.run(main())
