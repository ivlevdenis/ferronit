"""Сетевой бенчмарк: Ferronit vs FastAPI через uvicorn, 1000 маршрутов."""
import subprocess
import sys
import time
from pathlib import Path

import httpx

VENV_PY = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python"
ROUTES = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
N_REQUESTS = 5000
WARMUP = 200
RUNS = 3  # прогонов, берём медиану
PORTS = {"ferronit": 8111, "fastapi": 8112}

APP_CODE = {
    "ferronit": """
from ferronit import Ferronit
app = Ferronit()
for i in range(%(routes)d):
    exec(f'@app.route("/route{i}")\\ndef h{i}(req): return {{"route": {i}}}')
""",
    "fastapi": """
from fastapi import FastAPI
app = FastAPI()
for i in range(%(routes)d):
    exec(f'@app.get("/route{i}")\\ndef h{i}(): return {{"route": {i}}}')
""",
}


def start_server(name: str, port: int) -> subprocess.Popen:
    app_file = Path(__file__).parent / f"_bench_app_{name}.py"
    app_file.write_text(APP_CODE[name] % {"routes": ROUTES})
    return subprocess.Popen(
        [str(VENV_PY), "-m", "uvicorn", f"_bench_app_{name}:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=Path(__file__).parent,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def wait_ready(url: str, timeout: float = 15.0) -> None:
    t0 = time.monotonic()
    with httpx.Client(trust_env=False) as c:
        while time.monotonic() - t0 < timeout:
            try:
                if c.get(url, timeout=1).status_code == 200:
                    return
            except Exception:
                time.sleep(0.15)
    raise TimeoutError(f"{url} не поднялся")


def bench(url: str) -> float:
    paths = [f"/route{i}" for i in range(ROUTES)]
    results = []
    with httpx.Client(base_url=url, trust_env=False) as c:
        for _ in range(RUNS):
            for i in range(WARMUP):
                c.get(paths[i % ROUTES])
            t0 = time.perf_counter()
            for i in range(N_REQUESTS):
                c.get(paths[i % ROUTES])
            dt = time.perf_counter() - t0
            results.append(N_REQUESTS / dt)
    results.sort()
    return results[len(results) // 2]  # медиана


def main() -> None:
    procs = {name: start_server(name, port) for name, port in PORTS.items()}
    try:
        for name, port in PORTS.items():
            wait_ready(f"http://127.0.0.1:{port}/route0")
        results = {}
        for name, port in PORTS.items():
            results[name] = bench(f"http://127.0.0.1:{port}")
            print(f"{name:8s} {results[name]:10,.0f} req/s", flush=True)
        gain = results["ferronit"] / results["fastapi"] - 1
        print(f"\nFerronit быстрее FastAPI на {gain*100:.0f}%  ({ROUTES} маршрутов)")
    finally:
        for p in procs.values():
            p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
