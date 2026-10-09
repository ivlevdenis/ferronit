"""Сетевой бенч: Ferronit vs FastAPI с разным размером ответа (small/medium/big)."""
import subprocess
import sys
import time
from pathlib import Path

import httpx

VENV_PY = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python"
RUNS = 3
PORTS = {"ferronit": 8121, "fastapi": 8122}

# N запросов на каждый размер ответа
N_PER_SIZE = {"small": 5000, "medium": 2000, "big": 1000}
WARMUP = 100

PAYLOADS = """
SMALL = {"ok": True, "route": 0}
MEDIUM = [
    {
        "id": i,
        "name": f"user_{i}",
        "active": i % 2 == 0,
        "score": i * 1.5,
        "tags": [f"tag_{j}" for j in range(5)],
        "meta": {"level": i % 10, "role": "admin" if i % 3 == 0 else "user"},
    }
    for i in range(50)
]
BIG = [
    {
        "id": i,
        "name": f"user_{i}",
        "active": i % 2 == 0,
        "score": i * 1.5,
        "tags": [f"tag_{j}" for j in range(5)],
        "meta": {"level": i % 10, "role": "admin" if i % 3 == 0 else "user"},
    }
    for i in range(500)
]
"""

APP_CODE = {
    "ferronit": """
from ferronit import Ferronit
app = Ferronit()
""" + PAYLOADS + """

@app.route("/small")
def small(req):
    return SMALL

@app.route("/medium")
def medium(req):
    return MEDIUM

@app.route("/big")
def big(req):
    return BIG
""",
    "fastapi": """
from fastapi import FastAPI
app = FastAPI()
""" + PAYLOADS + """

@app.get("/small")
async def small():
    return SMALL

@app.get("/medium")
async def medium():
    return MEDIUM

@app.get("/big")
async def big():
    return BIG
""",
}


def start_server(name: str, port: int) -> subprocess.Popen:
    app_file = Path(__file__).parent / f"_bench_payload_{name}.py"
    app_file.write_text(APP_CODE[name])
    return subprocess.Popen(
        [str(VENV_PY), "-m", "uvicorn", f"_bench_payload_{name}:app",
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


def bench(url: str, path: str, n: int) -> float:
    results = []
    with httpx.Client(base_url=url, trust_env=False) as c:
        for _ in range(RUNS):
            for _ in range(WARMUP):
                c.get(path)
            t0 = time.perf_counter()
            for _ in range(n):
                c.get(path)
            dt = time.perf_counter() - t0
            results.append(n / dt)
    results.sort()
    return results[len(results) // 2]  # медиана


def main() -> None:
    procs = {name: start_server(name, port) for name, port in PORTS.items()}
    try:
        for name, port in PORTS.items():
            wait_ready(f"http://127.0.0.1:{port}/small")
        results = {}
        for size in ("small", "medium", "big"):
            results[size] = {}
            for name, port in PORTS.items():
                results[size][name] = bench(f"http://127.0.0.1:{port}", f"/{size}", N_PER_SIZE[size])

        print(f"{'size':8s} {'ferronit':>10s} {'fastapi':>10s} {'gain':>8s}")
        for size in ("small", "medium", "big"):
            v, f = results[size]["ferronit"], results[size]["fastapi"]
            gain = (v / f - 1) * 100
            print(f"{size:8s} {v:10,.0f} {f:10,.0f} {gain:+7.0f}%")
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
