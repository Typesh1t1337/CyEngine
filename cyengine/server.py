import asyncio
import os
import signal

try:
    import uvloop
    _UVLOOP = True
except ImportError:
    uvloop = None
    _UVLOOP = False


async def serve(app, host: str, port: int, backlog: int = 2048, grace: float = 10.0):
    """
    Поднять сервер и блокироваться до SIGINT/SIGTERM.

    После сигнала: перестаёт принимать соединения, ждёт текущие запросы
    (до `grace` секунд), закрывает keep-alive соединения и возвращает управление —
    дальше в main() отрабатывает код закрытия зависимостей.
    """
    loop = asyncio.get_running_loop()
    app._router = app._build_router()

    server = await loop.create_server(
        lambda: app._make_protocol(loop),
        host=host,
        port=port,
        reuse_port=True,
        backlog=backlog,
    )

    addr = server.sockets[0].getsockname()
    print(f"  cyengine  http://{addr[0]}:{addr[1]}")
    # реальный loop, а не «установлен ли uvloop»: serve() работает в том loop, где его вызвали
    loop_name = type(loop).__module__.split(".")[0]
    hint = "" if loop_name == "uvloop" else "  (uvloop быстрее: uvloop.run(main()))"
    print(f"  loop      {loop_name}{hint}")
    print(f"  pid       {os.getpid()}")

    sweeper = asyncio.create_task(_sweep(app, loop))

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        sweeper.cancel()
        await _shutdown(app, server, grace)


async def _sweep(app, loop):
    """
    Один сторожевой таймер на весь сервер вместо таймеров на каждый запрос:
    раз в `interval` проверяет дедлайны read/keep-alive у соединений и
    handler_timeout у запросов. На горячем пути остаётся только запись числа.
    """
    lim = app._limits
    active = [t for t in (lim.read_timeout, lim.keepalive_timeout, lim.handler_timeout)
              if t is not None]
    if not active:
        return
    interval = min(1.0, max(0.05, min(active) / 4))

    while True:
        await asyncio.sleep(interval)
        now = loop.time()
        for task, deadline in tuple(app._tasks.items()):
            if deadline and now >= deadline and task not in app._timed_out:
                app._timed_out.add(task)
                task.cancel()
        for proto in tuple(app._protocols):
            proto._check_deadline(now)


async def _shutdown(app, server, grace: float):
    server.close()

    if app._tasks:
        _, pending = await asyncio.wait(set(app._tasks), timeout=grace)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    for proto in list(app._protocols):
        proto.close()

    try:
        await asyncio.wait_for(server.wait_closed(), timeout=5)
    except asyncio.TimeoutError:
        pass


def run(app, host: str = "0.0.0.0", port: int = 8000):
    """Sync-обёртка для приложений без зависимостей, которым нужен close."""
    coro = serve(app, host, port)
    if _UVLOOP:
        uvloop.run(coro)
    else:
        asyncio.run(coro)
