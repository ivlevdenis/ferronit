"""Ping + access-log overhead across frameworks (ab, granian, 1 worker).

Each framework serves ``GET /ping`` and, when ``BENCH_ACCESS_LOG=1``, logs one
access line per request to stdout. granian's stdout is redirected to /dev/null,
so we measure the framework's logging cost on the request path, not terminal I/O.

- ``ferronit`` — its non-blocking ``Logger`` (JSON, background writer thread).
- ``ferronit-stdlib`` — Ferronit with ``StdlibLogger`` (stdlib ``logging``), for an
  apples-to-apples comparison with the other frameworks.
- ``blacksheep`` / ``litestar`` / ``fastapi`` / ``django`` — stdlib ``logging``
  access middleware, one line per request.

Запуск: .venv/bin/python bench/api_bench_logging.py
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV_PY = HERE.parent / ".venv" / "bin" / "python"

N = 10000
C = 50
RUNS = 3
SERVER = os.environ.get("BENCH_SERVER", "granian")

FERRONIT_APP = '''\
import os
import sys

from ferronit import Ferronit
from ferronit.contrib.logging import Logger, LogConfig

_access_log = os.environ.get("BENCH_ACCESS_LOG") == "1"
app = Ferronit(
    logger=Logger(LogConfig(stream=sys.stdout)) if _access_log else None,
    access_log=_access_log,
)


@app.route("/ping")
async def ping():
    return {"ok": True}
'''

FERRONIT_STDLIB_APP = '''\
import logging
import os
import sys

from ferronit import Ferronit
from ferronit.contrib.logging import StdlibLogger

_access_log = os.environ.get("BENCH_ACCESS_LOG") == "1"
if _access_log:
    _logger = logging.getLogger("ferronit")
    _logger.setLevel(logging.INFO)
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)
    _logger.propagate = False

app = Ferronit(logger=StdlibLogger("ferronit"), access_log=_access_log)


@app.route("/ping")
async def ping():
    return {"ok": True}
'''

BLACKSHEEP_APP = '''\
import logging
import os
import sys
import time

from blacksheep import Application

_access_log = os.environ.get("BENCH_ACCESS_LOG") == "1"
app = Application()

if _access_log:
    _logger = logging.getLogger("bench.access")
    _logger.setLevel(logging.INFO)
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)
    _logger.propagate = False

    async def _access_log(request, handler):
        _start = time.perf_counter()
        response = await handler(request)
        _logger.info(
            "%s %s %.3fms",
            request.method,
            response.status,
            (time.perf_counter() - _start) * 1000,
        )
        return response

    app.middlewares.append(_access_log)


@app.router.get("/ping")
async def ping():
    return {"ok": True}
'''

LITESTAR_APP = '''\
import logging
import os
import sys
import time

from litestar import Litestar, get
from litestar.types import ASGIApp, Receive, Scope, Send

_access_log = os.environ.get("BENCH_ACCESS_LOG") == "1"

if _access_log:
    _logger = logging.getLogger("bench.access")
    _logger.setLevel(logging.INFO)
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)
    _logger.propagate = False

    def _access_log(app: ASGIApp):
        async def _mw(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http":
                await app(scope, receive, send)
                return
            _start = time.perf_counter()
            _status = 200

            async def _sw(message):
                nonlocal _status
                if message["type"] == "http.response.start":
                    _status = message["status"]
                await send(message)

            await app(scope, receive, _sw)
            _logger.info(
                "%s %s %d %.3fms",
                scope["method"],
                scope["path"],
                _status,
                (time.perf_counter() - _start) * 1000,
            )

        return _mw


@get("/ping")
async def ping() -> dict:
    return {"ok": True}


app = Litestar(route_handlers=[ping], middleware=[_access_log] if _access_log else [])
'''

FASTAPI_APP = '''\
import logging
import os
import sys
import time

from fastapi import FastAPI

_access_log = os.environ.get("BENCH_ACCESS_LOG") == "1"
app = FastAPI()

if _access_log:
    _logger = logging.getLogger("bench.access")
    _logger.setLevel(logging.INFO)
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)
    _logger.propagate = False

    @app.middleware("http")
    async def _access_log(request, call_next):
        _start = time.perf_counter()
        response = await call_next(request)
        _logger.info(
            "%s %s %d %.3fms",
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - _start) * 1000,
        )
        return response


@app.get("/ping")
async def ping():
    return {"ok": True}
'''

APPS = {
    "ferronit": FERRONIT_APP,
    "ferronit-stdlib": FERRONIT_STDLIB_APP,
    "blacksheep": BLACKSHEEP_APP,
    "litestar": LITESTAR_APP,
    "fastapi": FASTAPI_APP,
}

# RSGI-стек — оба варианта Ferronit; остальные — ASGI.
RSGI = {"ferronit", "ferronit-stdlib"}

PORTS = {
    "ferronit": 8361,
    "ferronit-stdlib": 8366,
    "blacksheep": 8362,
    "litestar": 8363,
    "fastapi": 8364,
    "django": 8365,
}


def start(name: str, port: int, access_log: bool) -> subprocess.Popen:
    """Пишет приложение (кроме Django) и поднимает его granian'ом на выбранном порту."""
    env = {**os.environ, "BENCH_ACCESS_LOG": "1" if access_log else "0"}
    if name == "django":
        env["DJANGO_SETTINGS_MODULE"] = "django_bench.settings"
        cmd = [
            str(VENV_PY), "-m", SERVER, "--interface", "asgi",
            "--host", "127.0.0.1", "--port", str(port), "--workers", "1",
            "--loop", "uvloop", "--no-ws", "--log-level", "warning",
            "django_bench.asgi:application",
        ]
    else:
        module = name.replace("-", "_")
        (HERE / f"_bench_log_{module}.py").write_text(APPS[name])
        iface = "rsgi" if name in RSGI else "asgi"
        cmd = [
            str(VENV_PY), "-m", SERVER, "--interface", iface,
            "--host", "127.0.0.1", "--port", str(port), "--workers", "1",
            "--loop", "uvloop", "--log-level", "warning",
        ]
        if iface == "asgi":
            cmd.append("--no-ws")
        cmd.append(f"_bench_log_{module}:app")
    return subprocess.Popen(
        cmd,
        cwd=HERE,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_ready(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise TimeoutError(f"сервер на :{port} не поднялся")


def ab_reqs(port: int) -> float:
    out = subprocess.run(
        ["ab", "-n", str(N), "-c", str(C), "-k", f"http://127.0.0.1:{port}/ping"],
        capture_output=True, text=True, timeout=300,
    ).stdout
    match = re.search(r"Requests per second:\s+([\d.]+)", out)
    if not match:
        raise RuntimeError(f"ab не дал результат:\n{out[-400:]}")
    return float(match.group(1))


def best_of(port: int) -> float:
    best = 0.0
    for _ in range(RUNS):
        best = max(best, ab_reqs(port))
    return best


def main() -> None:
    names = list(PORTS)
    print(f"GET /ping, granian {SERVER}, 1 воркер, ab -n {N} -c {C} -k, лучшее из {RUNS}")
    print(f"{'framework':<16} {'no log':>12} {'log':>12} {'overhead':>10}")
    base: dict[str, float] = {}
    logged: dict[str, float] = {}
    for name, port in PORTS.items():
        proc = start(name, port, False)
        try:
            wait_ready(port)
            base[name] = best_of(port)
        finally:
            proc.terminate()
            proc.wait()
        time.sleep(0.5)

        proc = start(name, port, True)
        try:
            wait_ready(port)
            logged[name] = best_of(port)
        finally:
            proc.terminate()
            proc.wait()
        time.sleep(0.5)

    for name in names:
        b, l = base[name], logged[name]
        overhead = (l / b - 1) * 100
        print(f"{name:<16} {b:>12,.0f} {l:>12,.0f} {overhead:>9.1f}%")


def _cleanup() -> None:
    for name in APPS:
        module = name.replace("-", "_")
        (HERE / f"_bench_log_{module}.py").unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    finally:
        _cleanup()
