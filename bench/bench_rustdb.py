"""Замер Ferronit + ferronit.db (запросы в Rust, JSON на выходе) против Ferronit + asyncpg.

Протокол тот же, что у bench_postgres.py: `ab -c 50 -k`, GET /users 5000,
POST /users 3000, GET /ping 10000, granian, 1 и 8 воркеров.

Запуск: .venv/bin/python bench/bench_rustdb.py
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV_PY = HERE.parent / ".venv" / "bin" / "python"
APP_MODULE = "_bench_rustdb_app"
PORT = 8221
BODY = Path("/tmp/_bench_rustdb_post.json")


def ab_reqs(path: str, method: str = "GET", n: int = 5000) -> float:
    cmd = ["ab", "-n", str(n), "-c", "50", "-k"]
    if method == "POST":
        BODY.write_text('{"name": "bench", "email": "bench@example.com"}')
        cmd += ["-p", str(BODY), "-T", "application/json"]
    cmd.append(f"http://127.0.0.1:{PORT}{path}")
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300).stdout
    match = re.search(r"Requests per second:\s+([\d.]+)", out)
    if not match:
        raise RuntimeError(f"ab не дал результат для {path}:\n{out[-400:]}")
    return float(match.group(1))


def serve(workers: int) -> subprocess.Popen:
    return subprocess.Popen(
        [
            str(VENV_PY), "-m", "granian", "--interface", "asgi",
            "--host", "127.0.0.1", "--port", str(PORT), "--workers", str(workers),
            "--no-ws", "--log-level", "warning", f"{APP_MODULE}:app",
        ],
        cwd=HERE,
    )


def wait_ready(proc: subprocess.Popen) -> None:
    import urllib.request

    for _ in range(60):
        if proc.poll() is not None:
            raise RuntimeError("granian упал при старте")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/ping", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("granian не поднялся за 15 с")


def main() -> None:
    print(f"{'операция':14s} {'1 воркер':>10s} {'8 воркеров':>10s}")
    results: dict[int, dict[str, float]] = {1: {}, 8: {}}
    for workers in (1, 8):
        proc = serve(workers)
        try:
            wait_ready(proc)
            results[workers]["GET /users"] = ab_reqs("/users", "GET", 5000)
            results[workers]["POST /users"] = ab_reqs("/users", "POST", 3000)
            results[workers]["GET /ping"] = ab_reqs("/ping", "GET", 10000)
        finally:
            proc.terminate()
            proc.wait()
            time.sleep(1)
    for label in ("GET /users", "POST /users", "GET /ping"):
        print(f"{label:14s} {results[1][label]:>10,.0f} {results[8][label]:>10,.0f}")


if __name__ == "__main__":
    main()
