"""Чистый granian без фреймворка: пустой ответ и маленький JSON.

Потолок сервера — верхняя граница для всех бенчей (ferronit/litestar/fastapi
работают поверх granian). ``/ping`` у ferronit возвращает ``{"ok": true}``,
поэтому ``granian-json`` — прямой ориентир для него.

Запуск: .venv/bin/python bench/bench_granian_bare.py
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV_PY = HERE.parent / ".venv" / "bin" / "python"
SERVER = os.environ.get("BENCH_SERVER", "granian")
REQUESTS = 10000
RUNS = 2

APPS = {
    "granian-empty": (
        "async def app(scope, receive, send):\n"
        '    assert scope["type"] == "http"\n'
        '    await send({"type": "http.response.start", "status": 200, "headers": []})\n'
        '    await send({"type": "http.response.body", "body": b""})\n'
    ),
    "granian-json": (
        "async def app(scope, receive, send):\n"
        '    assert scope["type"] == "http"\n'
        '    await send({"type": "http.response.start", "status": 200,'
        ' "headers": [(b"content-type", b"application/json")]})\n'
        '    await send({"type": "http.response.body", "body": b\'{"ok": true}\'})\n'
    ),
}
PORTS = {
    "granian-empty": 8241,
    "granian-json": 8242,
}


def start(name: str, port: int) -> subprocess.Popen:
    (HERE / f"_bench_granian_{name}.py").write_text(APPS[name])
    cmd = [
        str(VENV_PY), "-m", SERVER, "--interface", "asgi",
        "--host", "127.0.0.1", "--port", str(port), "--workers", "1",
        "--no-ws", "--log-level", "warning", f"_bench_granian_{name}:app",
    ]
    return subprocess.Popen(
        cmd, cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def wait_ready(port: int, timeout: float = 20.0) -> None:
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise TimeoutError(f"сервер на :{port} не поднялся")


def ab_reqs(port: int, n: int) -> float:
    out = subprocess.run(
        ["ab", "-n", str(n), "-c", "50", "-k", f"http://127.0.0.1:{port}/"],
        capture_output=True, text=True, timeout=300,
    ).stdout
    match = re.search(r"Requests per second:\s+([\d.]+)", out)
    if not match:
        raise RuntimeError(f"ab не дал результат:\n{out[-400:]}")
    return float(match.group(1))


def main() -> None:
    print(f"чистый granian {SERVER}, 1 воркер, ab -c 50 -k, лучшее из {RUNS}\n")
    procs = {name: start(name, port) for name, port in PORTS.items()}
    try:
        for port in PORTS.values():
            wait_ready(port)
        best: dict[str, float] = {}
        for _ in range(RUNS):
            for name, port in PORTS.items():
                best[name] = max(best.get(name, 0.0), ab_reqs(port, REQUESTS))
        for name in PORTS:
            print(f"  {name:16s} {best[name]:>12,.0f} req/s")
    finally:
        for proc in procs.values():
            proc.terminate()
        for proc in procs.values():
            proc.wait()


if __name__ == "__main__":
    main()
