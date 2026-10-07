"""Ferrox vs FastAPI vs Litestar: обработка входных параметров.

Три одинаковых эндпоинта — `/ping` (без параметров), `/search` (три типизированных
query-параметра), `/users/{id}` (типизированный path-параметр) — на одном сервере
(granian, 1 воркер), замер через `ab`. Перед замером проверяется корректность ответов.

Запуск: .venv/bin/python bench/bench_params.py
Результат — таблица в stdout + SVG-график `bench_params.html`.
"""

from __future__ import annotations

import json
import math
import statistics
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from bench_rows import VENV_PY, ab_reqs, wait_ready  # noqa: E402

APPS = {
    "ferrox": '''
from ferrox import Ferrox
app = Ferrox()

@app.route("/ping")
async def ping(req):
    return {"ok": True}

@app.route("/search")
async def search(req, q: str = "", limit: int = 10, page: int = 1):
    return {"q": q, "limit": limit, "page": page}

@app.route("/users/{user_id}")
async def get_user(req, user_id: int):
    return {"id": user_id}
''',
    "fastapi": '''
from fastapi import FastAPI
app = FastAPI()

@app.get("/ping")
async def ping():
    return {"ok": True}

@app.get("/search")
async def search(q: str = "", limit: int = 10, page: int = 1):
    return {"q": q, "limit": limit, "page": page}

@app.get("/users/{user_id}")
async def get_user(user_id: int):
    return {"id": user_id}
''',
    "litestar": '''
from litestar import Litestar, get

@get("/ping")
async def ping() -> dict:
    return {"ok": True}

@get("/search")
async def search(q: str = "", limit: int = 10, page: int = 1) -> dict:
    return {"q": q, "limit": limit, "page": page}

@get("/users/{user_id:int}")
async def get_user(user_id: int) -> dict:
    return {"id": user_id}

app = Litestar(route_handlers=[ping, search, get_user])
''',
}

PORTS = {"ferrox": 8281, "fastapi": 8282, "litestar": 8283}
ROUTES = [
    ("/ping", 20000),
    ("/search?q=hello&limit=25&page=2", 20000),
    ("/users/42", 20000),
]
CHECKS = {
    "/search?q=hello&limit=25&page=2": {"q": "hello", "limit": 25, "page": 2},
    "/users/42": {"id": 42},
}
ROUTE_LABELS = {
    "/ping": "/ping (без параметров)",
    "/search?q=hello&limit=25&page=2": "/search (3 query-параметра)",
    "/users/42": "/users/42 (path-параметр)",
}
COLORS = {"ferrox": "#6ea8fe", "fastapi": "#ff7b72", "litestar": "#5ee0d0"}


def start(name: str, port: int) -> subprocess.Popen:
    (HERE / f"_bench_params_{name}.py").write_text(APPS[name])
    return subprocess.Popen(
        [str(VENV_PY), "-m", "granian", "--interface", "asgi", "--host", "127.0.0.1",
         "--port", str(port), "--workers", "1", "--no-ws", "--log-level", "warning",
         f"_bench_params_{name}:app"],
        cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def check(port: int, path: str, expected: dict) -> None:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
        body = json.loads(resp.read())
    assert body == expected, f"{path}: {body} != {expected}"


def fmt(n: float) -> str:
    return f"{round(n):,}".replace(",", "\u202f")


def _log_x(value: float, x0: float, x1: float) -> float:
    return x0 + math.log10(value) / 5.0 * (x1 - x0)  # 1 .. 100 000


def render_svg(results: dict[str, dict[str, float]]) -> str:
    """Сгруппированные горизонтальные полосы: 3 фреймворка × 3 маршрута."""
    width, label_x, x0, x1 = 900, 170, 190, 880
    group_h, bar_h, top = 52, 13, 20
    order = ["ferrox", "litestar", "fastapi"]
    height = top + len(order) * group_h + 30

    parts = [f'<rect width="{width}" height="{height}" rx="12" fill="#0f141c"/>']
    for tick in (1_000, 10_000, 100_000):
        x = _log_x(tick, x0, x1)
        parts.append(f'<line x1="{x:.0f}" y1="{top}" x2="{x:.0f}" y2="{height-26}" stroke="#1e2836"/>')
        parts.append(f'<text x="{x:.0f}" y="{height-10}" text-anchor="middle" font-size="10" fill="#8ea0b5">{fmt(tick)}</text>')

    for i, name in enumerate(order):
        cy = top + i * group_h + group_h / 2
        parts.append(f'<text x="{label_x}" y="{cy+4}" text-anchor="end" font-size="12.5" fill="#dfe7f0">{name}</text>')
        for r, (path, _) in enumerate(ROUTES):
            value = results[name][path]
            bx = _log_x(value, x0, x1)
            by = cy + (r - 1) * 15
            parts.append(f'<rect x="{x0}" y="{by-bar_h/2:.0f}" width="{bx-x0:.0f}" height="{bar_h}" rx="2" fill="{COLORS[name]}" opacity="{0.5+0.25*r:.2f}"/>')
            parts.append(f'<text x="{bx+6:.0f}" y="{by+4}" font-size="10.5" font-weight="600" fill="#eaf1f9">{fmt(value)}</text>')

    legend = "".join(
        f'<rect x="{i*110}" y="{height-4}" width="9" height="9" rx="2" fill="#8892a5"/>'
        f'<text x="{i*110+13}" y="{height+4}" font-size="10" fill="#8ea0b5">{label}</text>'
        for i, label in enumerate(["ping", "search", "users"])
    )
    parts.append(legend)
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" xmlns="http://www.w3.org/2000/svg">'
        + "".join(parts) + "</svg>"
    )


def main() -> None:
    procs = {name: start(name, port) for name, port in PORTS.items()}
    try:
        for port in PORTS.values():
            wait_ready(port)
        for port in PORTS.values():
            for path, expected in CHECKS.items():
                check(port, path, expected)
        print("корректность ответов: ок\n")

        results: dict[str, dict[str, float]] = {n: {} for n in PORTS}
        header = f"{'маршрут':34s} " + " ".join(f"{n:>9}" for n in PORTS) + "   (req/s)"
        print(header + "\n" + "-" * (34 + 11 * len(PORTS)))
        for path, n in ROUTES:
            cells = []
            for name, port in PORTS.items():
                median = statistics.median([ab_reqs(port, path, n) for _ in range(4)])
                results[name][path] = median
                cells.append(f"{median:>9,.0f}")
            print(f"{ROUTE_LABELS[path]:34s} " + " ".join(cells))

        html = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Ferrox — параметры</title>
<style>body{{margin:0;padding:30px 40px;background:#0b0e14;color:#dfe7f0;font-family:Segoe UI,system-ui,sans-serif}}
h1{{font-size:20px;color:#eaf1f9;margin:0 0 4px}}.sub{{color:#8ea0b5;font-size:13px;margin:0 0 20px}}
.card{{background:#0f141c;border:1px solid #1e2836;border-radius:14px;padding:16px 20px;max-width:980px}}</style></head>
<body><h1>Ferrox vs FastAPI vs Litestar — обработка параметров</h1>
<p class="sub">granian · 1 воркер · ab -c 50 -k · req/s (логарифмическая шкала)</p>
<div class="card">{render_svg(results)}</div></body></html>"""
        (HERE / "bench_params.html").write_text(html, encoding="utf-8")
        print("\ngrafik: bench/bench_params.html")
    finally:
        for proc in procs.values():
            proc.terminate()
        for proc in procs.values():
            proc.wait()
        for name in PORTS:
            (HERE / f"_bench_params_{name}.py").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
