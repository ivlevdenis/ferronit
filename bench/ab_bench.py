"""Единственный источник цифр по HTTP: ApacheBench (C-клиент, `ab`).

Почему только ab: httpx/asyncio-клиент сам упирается в ~3.5–3.7k req/s независимо от
сервера, поэтому httpx-замеры говорят о клиенте, а не о фреймворке. ab написан на C и
не создаёт это бутылочное горлышко.

Что делает: генерирует два одинаковых приложения (Ferronit и FastAPI) с N маршрутами,
поднимает их одним и тем же сервером (uvicorn или granian, 1 воркер), прогревает и
мерит ab по выборке маршрутов (первый / средний / последний) — так видно и то, что
FastAPI деградирует с ростом таблицы маршрутов, и то, что Ferronit остаётся плоским.

    python bench/ab_bench.py --server uvicorn --routes 50
    python bench/ab_bench.py --server granian  --routes 1000 --requests 20000
    python bench/ab_bench.py --server uvicorn --payload big --requests 5000
    python bench/ab_bench.py --server uvicorn --routes 50 --no-fastapi

Результаты — в stdout (таблица); ничего не пишется в файлы, кроме временных приложений.
"""

from __future__ import annotations

import argparse
import re
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
ROOT = BENCH_DIR.parent
PY = str(ROOT / ".venv" / "bin" / "python")

FERRONIT_TEMPLATE = '''
from ferronit import Ferronit

app = Ferronit()


def _make(i):
    def handler(req):
        return {"route": i}
    return handler


for _i in range(__ROUTES__):
    app.route(f"/route{_i}")(_make(_i))

__PAYLOAD__
'''

FASTAPI_TEMPLATE = '''
from fastapi import FastAPI

app = FastAPI()


def _make(i):
    @app.get(f"/route{i}")
    def handler():
        return {"route": i}
    return handler


for _i in range(__ROUTES__):
    _make(_i)

__PAYLOAD__
'''

PAYLOAD_BODY = {
    "none": "",
    "small": '''
@app.route("/payload")
def payload(req):
    return {"ok": True}
''',
    "medium": '''
_ITEMS = [{"id": i, "name": f"item-{i}", "price": i * 10} for i in range(50)]


@app.route("/payload")
def payload(req):
    return {"count": 50, "items": _ITEMS}
''',
    "big": '''
_ITEMS = [{"id": i, "name": f"item-{i}", "price": i * 10} for i in range(500)]


@app.route("/payload")
def payload(req):
    return {"count": 500, "items": _ITEMS}
''',
}

FASTAPI_PAYLOAD_BODY = {
    "none": "",
    "small": '''
@app.get("/payload")
def payload():
    return {"ok": True}
''',
    "medium": '''
_ITEMS = [{"id": i, "name": f"item-{i}", "price": i * 10} for i in range(50)]


@app.get("/payload")
def payload():
    return {"count": 50, "items": _ITEMS}
''',
    "big": '''
_ITEMS = [{"id": i, "name": f"item-{i}", "price": i * 10} for i in range(500)]


@app.get("/payload")
def payload():
    return {"count": 500, "items": _ITEMS}
''',
}

RPS_RE = re.compile(r"Requests per second:\s+([\d.]+)")
FAILED_RE = re.compile(r"Failed requests:\s+(\d+)")
NON2XX_RE = re.compile(r"Non-2xx responses:\s+(\d+)")
PCT_RE = re.compile(r"^\s+(\d+)%\s+(\d+)\s*$", re.MULTILINE)


def write_apps(routes: int, payload: str) -> dict[str, str]:
    """Generate the two apps on disk; return {module_name: path}."""
    written: dict[str, str] = {}
    for name, template, payloads in (
        ("ferronit", FERRONIT_TEMPLATE, PAYLOAD_BODY),
        ("fastapi", FASTAPI_TEMPLATE, FASTAPI_PAYLOAD_BODY),
    ):
        source = template.replace("__ROUTES__", str(routes)).replace("__PAYLOAD__", payloads[payload])
        path = BENCH_DIR / f"_abgen_{name}.py"
        path.write_text(source, encoding="utf-8")
        written[name] = path.name
    return written


def server_cmd(server: str, module: str, port: int) -> list[str]:
    """Build the server command line for the requested backend."""
    if server == "granian":
        return [PY, "-m", "granian", "--interface", "asgi", "--host", "127.0.0.1",
                "--port", str(port), "--workers", "1", "--no-ws", f"{module}:app"]
    return [PY, "-m", "uvicorn", f"{module}:app", "--host", "127.0.0.1",
            "--port", str(port), "--log-level", "warning"]


def wait_ready(port: int, timeout: float = 20.0) -> bool:
    """Wait until the server accepts TCP connections (no HTTP client involved)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def run_ab(url: str, requests: int, concurrency: int) -> dict:
    """Run one ab measurement and parse the numbers we report."""
    proc = subprocess.run(
        ["ab", "-n", str(requests), "-c", str(concurrency), "-k", url],
        capture_output=True, text=True,
    )
    out = proc.stdout
    if proc.returncode != 0:
        return {"error": (proc.stderr or out).strip().splitlines()[-1:] or ["ab failed"]}
    rps = RPS_RE.search(out)
    failed = FAILED_RE.search(out)
    non2xx = NON2XX_RE.search(out)
    pct = {int(p): int(v) for p, v in PCT_RE.findall(out)}
    return {
        "rps": float(rps.group(1)) if rps else 0.0,
        "failed": int(failed.group(1)) if failed else -1,
        "non2xx": int(non2xx.group(1)) if non2xx else 0,
        "p50": pct.get(50), "p95": pct.get(95), "p99": pct.get(99),
    }


def sample_routes(routes: int) -> list[str]:
    """First / middle / last route — so a routing table scan cannot hide."""
    if routes <= 1:
        return ["/route0"]
    if routes <= 3:
        return [f"/route{i}" for i in range(routes)]
    return ["/route0", f"/route{routes // 2}", f"/route{routes - 1}"]


def measure(port: int, paths: list[str], requests: int, concurrency: int, runs: int) -> dict:
    """Warm up, then run each sampled path `runs` times and keep the median."""
    run_ab(f"http://127.0.0.1:{port}{paths[0]}", max(1000, requests // 10), concurrency)  # прогрев
    per_path = {}
    for path in paths:
        results = [run_ab(f"http://127.0.0.1:{port}{path}", requests, concurrency) for _ in range(runs)]
        good = [r for r in results if "error" not in r]
        if not good:
            per_path[path] = {"error": results[0].get("error")}
            continue
        per_path[path] = {
            "rps": statistics.median(r["rps"] for r in good),
            "p50": statistics.median(r["p50"] for r in good if r["p50"] is not None),
            "p95": statistics.median(r["p95"] for r in good if r["p95"] is not None),
            "p99": statistics.median(r["p99"] for r in good if r["p99"] is not None),
            "failed": sum(r["failed"] for r in good),
            "non2xx": sum(r["non2xx"] for r in good),
        }
    median_rps = statistics.median(v["rps"] for v in per_path.values() if "rps" in v)
    return {"paths": per_path, "median_rps": median_rps}


def main() -> int:
    ap = argparse.ArgumentParser(description="Ferronit vs FastAPI — только ApacheBench")
    ap.add_argument("--server", choices=["uvicorn", "granian"], default="uvicorn")
    ap.add_argument("--routes", type=int, default=50)
    ap.add_argument("--requests", type=int, default=20000)
    ap.add_argument("--concurrency", type=int, default=50)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--payload", choices=list(PAYLOAD_BODY), default="none")
    ap.add_argument("--no-fastapi", action="store_true", help="мерить только Ferronit")
    a = ap.parse_args()

    if subprocess.run(["which", "ab"], capture_output=True).returncode != 0:
        print("ab не найден: apt install apache2-utils", file=sys.stderr)
        return 1

    modules = write_apps(a.routes, a.payload)
    apps = [("ferronit", modules["ferronit"], 8161)]
    if not a.no_fastapi:
        apps.append(("fastapi", modules["fastapi"], 8162))

    target = "/payload" if a.payload != "none" else None
    paths = [target] if target else sample_routes(a.routes)

    print(f"ab -n {a.requests} -c {a.concurrency} -k · {a.server}, 1 воркер · "
          f"{a.routes} маршрутов · payload={a.payload} · медиана из {a.runs} прогонов")
    print(f"маршруты: {', '.join(paths)}\n")

    results: dict[str, dict] = {}
    for name, module, port in apps:
        proc = subprocess.Popen(server_cmd(a.server, module.removesuffix(".py"), port),
                                cwd=BENCH_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            if not wait_ready(port):
                print(f"{name}: сервер не поднялся", file=sys.stderr)
                continue
            results[name] = measure(port, paths, a.requests, a.concurrency, a.runs)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            time.sleep(0.5)

    for name, res in results.items():
        print(f"--- {name} ---")
        for path, data in res["paths"].items():
            if "error" in data:
                print(f"  {path:<14} ошибка ab: {data['error']}")
                continue
            print(f"  {path:<14} {data['rps']:>9,.0f} req/s   p50={data['p50']} p95={data['p95']} "
                  f"p99={data['p99']} мс   failed={data['failed']} non2xx={data['non2xx']}")
        print(f"  медиана: {res['median_rps']:,.0f} req/s")

    if "ferronit" in results and "fastapi" in results:
        v, f = results["ferronit"]["median_rps"], results["fastapi"]["median_rps"]
        print(f"\nИтого: Ferronit {v:,.0f} vs FastAPI {f:,.0f} req/s → ×{v / f:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
