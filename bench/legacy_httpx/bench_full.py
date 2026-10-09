"""Полный бенчмарк: Ferronit vs FastAPI — все сценарии в одном прогоне.

1. ASGI in-process (2 маршрута)
2. uvicorn 50 маршрутов
3. uvicorn 1000 маршрутов
4. uvicorn, размер ответа: small / medium / big
"""
import asyncio
import subprocess
import sys
import time
from pathlib import Path

import httpx
from httpx import ASGITransport, AsyncClient

from ferronit import Ferronit

VENV_PY = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python"
RUNS = 3
WARMUP = 200
PORTS = {"ferronit": 8131, "fastapi": 8132}

N_ROUTES = {"50": 5000, "1000": 5000}
N_PAYLOAD = {"small": 5000, "medium": 2000, "big": 1000}

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


# ── 1. ASGI in-process ────────────────────────────────────────────────

def make_ferronit():
    v = Ferronit()

    @v.route("/")
    def home(req):
        return {"ok": True}

    @v.route("/reflect")
    def reflect(req):
        return {"method": req.method, "path": req.path}

    return v


async def bench_asgi(client: AsyncClient, path: str, n: int) -> float:
    for _ in range(200):
        await client.get(path)
    results = []
    for _ in range(RUNS):
        t0 = time.perf_counter()
        for _ in range(n):
            await client.get(path)
        results.append(n / (time.perf_counter() - t0))
    results.sort()
    return results[len(results) // 2]


async def asgi_bench() -> dict:
    out = {}
    v = make_ferronit()
    async with AsyncClient(transport=ASGITransport(app=v), base_url="http://test") as c:
        out["ferronit /"] = await bench_asgi(c, "/", 5000)
        out["ferronit /reflect"] = await bench_asgi(c, "/reflect", 5000)

    from fastapi import FastAPI

    fa = FastAPI()

    @fa.get("/")
    async def home():
        return {"ok": True}

    @fa.get("/reflect")
    async def reflect(request):
        return {"method": request.method, "path": request.url.path}

    async with AsyncClient(transport=ASGITransport(app=fa), base_url="http://test") as c:
        out["fastapi /"] = await bench_asgi(c, "/", 5000)
        out["fastapi /reflect"] = await bench_asgi(c, "/reflect", 5000)
    return out


# ── 2-4. uvicorn-сценарии ─────────────────────────────────────────────

def make_app_code(kind: str, routes: int) -> str:
    if kind == "routes_big":
        return {
            "ferronit": """
from ferronit import Ferronit
app = Ferronit()
""" + PAYLOADS + """
for i in range(1000):
    def _make(i):
        def h(req, i=i):
            return BIG
        app.route(f"/route{i}")(h)
    _make(i)
""",
            "fastapi": """
from fastapi import FastAPI
app = FastAPI()
""" + PAYLOADS + """
for i in range(1000):
    def _make(i):
        def h(i=i):
            return BIG
        app.get(f"/route{i}")(h)
    _make(i)
""",
        }
    if kind == "routes":
        return {
            "ferronit": f"""
from ferronit import Ferronit
app = Ferronit()
for i in range({routes}):
    def _make(i):
        def h(req, i=i):
            return {{"route": i}}
        app.route(f"/route{{i}}")(h)
    _make(i)
""",
            "fastapi": f"""
from fastapi import FastAPI
app = FastAPI()
for i in range({routes}):
    def _make(i):
        def h(i=i):
            return {{"route": i}}
        app.get(f"/route{{i}}")(h)
    _make(i)
""",
        }
    return {
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


def start_server(name: str, port: int, code: str) -> subprocess.Popen:
    app_file = Path(__file__).parent / f"_bench_full_{name}.py"
    app_file.write_text(code)
    return subprocess.Popen(
        [str(VENV_PY), "-m", "uvicorn", f"_bench_full_{name}:app",
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


def bench_http(url: str, path: str, n: int, routes: int = 0) -> float:
    """Если routes > 0 — запросы идут по кругу по всем маршрутам (честный тест роутинга)."""
    paths = [f"/route{i}" for i in range(routes)] if routes else [path]
    results = []
    with httpx.Client(base_url=url, trust_env=False) as c:
        for _ in range(RUNS):
            for i in range(WARMUP):
                c.get(paths[i % len(paths)])
            t0 = time.perf_counter()
            for i in range(n):
                c.get(paths[i % len(paths)])
            results.append(n / (time.perf_counter() - t0))
    results.sort()
    return results[len(results) // 2]


def network_bench(routes: int) -> dict:
    code = make_app_code("routes", routes)
    procs = {name: start_server(name, port, code[name]) for name, port in PORTS.items()}
    try:
        for name, port in PORTS.items():
            wait_ready(f"http://127.0.0.1:{port}/route0")
        out = {}
        for name, port in PORTS.items():
            out[name] = bench_http(f"http://127.0.0.1:{port}", "/route0", N_ROUTES[str(routes)], routes=routes)
        return out
    finally:
        for p in procs.values():
            p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


def routes_big_bench() -> dict:
    """1000 маршрутов × big payload (500 объектов), запросы по кругу."""
    code = make_app_code("routes_big", 0)
    procs = {name: start_server(name, port, code[name]) for name, port in PORTS.items()}
    try:
        for name, port in PORTS.items():
            wait_ready(f"http://127.0.0.1:{port}/route0")
        out = {}
        for name, port in PORTS.items():
            out[name] = bench_http(f"http://127.0.0.1:{port}", "/route0", 1000, routes=1000)
        return out
    finally:
        for p in procs.values():
            p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


def payload_bench() -> dict:
    code = make_app_code("payload", 0)
    procs = {name: start_server(name, port, code[name]) for name, port in PORTS.items()}
    try:
        for name, port in PORTS.items():
            wait_ready(f"http://127.0.0.1:{port}/small")
        out = {}
        for size in ("small", "medium", "big"):
            out[size] = {}
            for name, port in PORTS.items():
                out[size][name] = bench_http(f"http://127.0.0.1:{port}", f"/{size}", N_PAYLOAD[size])
        return out
    finally:
        for p in procs.values():
            p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


def fmt_gain(v: float, f: float) -> str:
    gain = (v / f - 1) * 100
    return f"{gain:+7.0f}%"


def main() -> None:
    print("=== Полный бенчмарк Ferronit vs FastAPI ===")
    print(f"Python: {sys.version.split()[0]}, uvicorn, 1 worker, медиана из {RUNS} прогонов\n")

    # 1. ASGI in-process
    print("--- 1. ASGI in-process (2 маршрута) ---")
    asgi = asyncio.run(asgi_bench())
    print(f"  {'/':14s} Ferronit {asgi['ferronit /']:9,.0f}  FastAPI {asgi['fastapi /']:9,.0f}  {fmt_gain(asgi['ferronit /'], asgi['fastapi /'])}")
    print(f"  {'/reflect':14s} Ferronit {asgi['ferronit /reflect']:9,.0f}  FastAPI {asgi['fastapi /reflect']:9,.0f}  {fmt_gain(asgi['ferronit /reflect'], asgi['fastapi /reflect'])}")
    print()

    # 2-3. Network routes
    for routes in (50, 1000):
        print(f"--- 2. uvicorn {routes} маршрутов ---")
        res = network_bench(routes)
        print(f"  Ferronit {res['ferronit']:9,.0f}  FastAPI {res['fastapi']:9,.0f}  {fmt_gain(res['ferronit'], res['fastapi'])}")
        print()

    # 4. 1000 routes × big payload
    print("--- 4. uvicorn 1000 маршрутов × big payload (500 объектов) ---")
    rb = routes_big_bench()
    print(f"  Ferronit {rb['ferronit']:9,.0f}  FastAPI {rb['fastapi']:9,.0f}  {fmt_gain(rb['ferronit'], rb['fastapi'])}")
    print()

    # 5. Payload
    print("--- 5. uvicorn, размер ответа ---")
    pay = payload_bench()
    for size in ("small", "medium", "big"):
        v, f = pay[size]["ferronit"], pay[size]["fastapi"]
        print(f"  {size:8s} Ferronit {v:9,.0f}  FastAPI {f:9,.0f}  {fmt_gain(v, f)}")
    print()

    print("Готово.")


if __name__ == "__main__":
    main()
