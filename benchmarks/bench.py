"""
Нагрузка через bombardier: по каждому варианту поднимает сервер в отдельном
процессе, греет, меряет. Запуск (из корня): python benchmarks/bench.py [секунды]
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DURATION = sys.argv[1] if len(sys.argv) > 1 else "8"
CONNS = "64"
VARIANTS = (sys.argv[2].split(",") if len(sys.argv) > 2 else ["cy", "cy-nolimits", "cy-recover", "starlette"])
PATHS = sys.argv[3].split(",") if len(sys.argv) > 3 else ["/json", "/users/42"]
PORT = 9100


def wait_up(port):
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=0.5).read()
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("server did not start")


def bombard(port, path, duration):
    out = subprocess.run(
        ["bombardier", "-l", "-c", CONNS, "-d", f"{duration}s", "-o", "json", "-p", "r",
         f"http://127.0.0.1:{port}{path}"],
        capture_output=True, text=True, check=True).stdout
    r = json.loads(out)["result"]
    lat = r["latency"]
    return r["rps"]["mean"], lat["mean"] / 1000, lat["percentiles"]["99"] / 1000, r["req2xx"], r["others"]


rows = []
for i, variant in enumerate(VARIANTS):
    port = PORT + i
    env = dict(os.environ, PYTHONPATH=ROOT)
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "apps.py"), variant, str(port)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_up(port)
        for path in PATHS:
            bombard(port, path, 2)                       # прогрев
            rps, mean, p99, ok, bad = bombard(port, path, DURATION)
            rows.append((variant, path, rps, mean, p99, bad))
            print(f"{variant:12} {path:11} {rps:9.0f} rps  mean {mean:6.2f} ms  p99 {p99:6.2f} ms  non-2xx={bad}", flush=True)
    finally:
        proc.terminate()
        proc.wait()
