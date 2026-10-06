"""Сравнение режимов uvicorn: asyncio+h11 vs uvloop+httptools (Ferrox, 50 роутов)."""
import subprocess
import time
from pathlib import Path

import httpx

VENV_PY = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python"
N = 5000
WARMUP = 200

APP = """
from ferrox import Ferrox
app = Ferrox()
for i in range(50):
    def _make(i):
        def h(req, i=i):
            return {"route": i}
        app.route(f"/route{i}")(h)
    _make(i)
"""

MODES = [
    ("asyncio+h11 (default)", []),
    ("uvloop+h11", ["--loop", "uvloop"]),
    ("asyncio+httptools", ["--http", "httptools"]),
    ("uvloop+httptools", ["--loop", "uvloop", "--http", "httptools"]),
]


def bench(port: int) -> float:
    paths = [f"/route{i}" for i in range(50)]
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as c:
        for i in range(WARMUP):
            c.get(paths[i % 50])
        results = []
        for _ in range(3):
            t0 = time.perf_counter()
            for i in range(N):
                c.get(paths[i % 50])
            results.append(N / (time.perf_counter() - t0))
    results.sort()
    return results[len(results) // 2]


app_file = Path(__file__).parent / "_bench_mode_app.py"
app_file.write_text(APP)

for name, args in MODES:
    port = 8140 + MODES.index((name, args))
    p = subprocess.Popen(
        [str(VENV_PY), "-m", "uvicorn", "_bench_mode_app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning", *args],
        cwd=Path(__file__).parent,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # ждём готовности
        ready = False
        with httpx.Client(trust_env=False) as c:
            for _ in range(100):
                try:
                    if c.get(f"http://127.0.0.1:{port}/route0", timeout=1).status_code == 200:
                        ready = True
                        break
                except Exception:
                    time.sleep(0.15)
        if not ready:
            print(f"{name:22s} НЕ ПОДНЯЛСЯ")
            continue
        rps = bench(port)
        print(f"{name:22s} {rps:8,.0f} req/s")
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
