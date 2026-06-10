import asyncio
import os

import signal
import sys

try:
    import uvloop
    _UVLOOP = True
except ImportError:
    uvloop = None
    _UVLOOP = False


async def _serve(app, host: str, port: int):
    loop = asyncio.get_event_loop()

    server = await loop.create_server(
        lambda: app._make_protocol(loop),
        host=host,
        port=port,
        reuse_port=True,
        backlog=2048
    )

    addr = server.sockets[0].getsockname()
    print(f"  cyengine   http://{addr[0]}:{addr[1]}")
    print(f"  uvloop    {'yes' if _UVLOOP else 'no — pip install uvloop'}")
    print(f"  pid       {os.getpid()}")

    loop.add_signal_handler(signal.SIGTERM, server.close)
    loop.add_signal_handler(signal.SIGINT,  server.close)

    async with server:
        await server.serve_forever()


def run(app, host: str = "0.0.0.0", port: int = 8000, workers: int = 1):
    if workers > 1:
        _fork(app, host, port, workers)
    else:
        _single(app, host, port)


def _single(app, host, port):
    if _UVLOOP:
        uvloop.run(_serve(app, host, port))
    else:
        asyncio.run(_serve(app, host, port))


def _fork(app, host: str, port: int, workers: int):
    pids = []

    for _ in range(workers - 1):
        pid = os.fork()
        if pid == 0:
            _single(app, host, port)
            sys.exit(0)

        pids.append(pid)

    try:
        _single(app, host, port)
    finally:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

