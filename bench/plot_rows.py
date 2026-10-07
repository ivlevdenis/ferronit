"""Запуск bench_rows (1/100/1000) и построение красивого HTML-графика.

Использование:
    .venv/bin/python bench/plot_rows.py                 # прогнать бенч и построить
    .venv/bin/python bench/plot_rows.py results.json    # построить из готового JSON

График — самодостаточный SVG (без CDN и JS): страница статична, числа на полосах
всегда видны, работает офлайн. Формат results.json — то, что пишет ``run_case``:
{имя: {"rows": {1:.., 100:.., 1000:..}, "ping": ..}}.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bench_rows as bench  # noqa: E402

LABELS = {
    "ferrox-msgspec": "Ferrox + msgspec",
    "ferrox-model": "Ferrox + rawmodel",
    "ferrox-asyncpg": "Ferrox + asyncpg (dict)",
    "litestar-msgspec": "Litestar + msgspec",
    "litestar-asyncpg": "Litestar + asyncpg (dict)",
    "litestar-orm": "Litestar + ORM",
    "fastapi-orm": "FastAPI + ORM",
    "django-orm": "Django + ORM",
}
COLORS = {
    "ferrox-msgspec": "#7ee787",
    "ferrox-model": "#6ea8fe",
    "ferrox-asyncpg": "#79c0ff",
    "litestar-msgspec": "#5ee0d0",
    "litestar-asyncpg": "#a5d6ff",
    "litestar-orm": "#d2a8ff",
    "fastapi-orm": "#ff7b72",
    "django-orm": "#ffa657",
}
ROWS_SPEC = [(1, "#6ea8fe"), (100, "#7ee787"), (1000, "#f0b849")]

WIDTH = 1360
LABEL_X = 212
LEFT_X0, LEFT_X1 = 226, 800
RIGHT_X0, RIGHT_X1 = 976, 1332
TOP = 18
GROUP_H = 46
BAR_H = 12


def collect() -> dict:
    bench.seed()
    results = {name: {"rows": {}, "ping": None} for name in bench.PORTS}
    for rows in bench.ROWS_LIST:
        print(f"\n=== {rows} строк ===", flush=True)
        case = bench.run_case(rows)
        for name, r in case.items():
            results[name]["rows"][rows] = round(r["rows"])
            if results[name]["ping"] is None:
                results[name]["ping"] = round(r["ping"])
    return results


def load(path: str) -> dict:
    results = json.loads(Path(path).read_text(encoding="utf-8"))
    for name in results:
        results[name]["rows"] = {int(k): v for k, v in results[name]["rows"].items()}
    return results


def _fmt(n: float) -> str:
    return f"{round(n):,}".replace(",", "\u202f")


def _log_x(value: float, vmin: float, vmax: float, x0: float, x1: float) -> float:
    lmin, lmax = math.log10(vmin), math.log10(vmax)
    return x0 + (math.log10(value) - lmin) / (lmax - lmin) * (x1 - x0)


def _gridlines(x0: float, x1: float, ticks: list[int]) -> str:
    parts = []
    for tick in ticks:
        x = _log_x(tick, ticks[0], ticks[-1], x0, x1)
        parts.append(
            f'<line x1="{x:.1f}" y1="{TOP}" x2="{x:.1f}" y2="{TOP + len(LABELS) * GROUP_H}" '
            f'stroke="#1e2836" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{x:.1f}" y="{TOP + len(LABELS) * GROUP_H + 18}" text-anchor="middle" '
            f'font-size="11" fill="#8ea0b5">{_fmt(tick)}</text>'
        )
    return "".join(parts)


def _legend(x: float, y: float, items: list[tuple[str, str]]) -> str:
    parts = []
    cx = x
    for label, color in items:
        parts.append(f'<rect x="{cx}" y="{y - 7}" width="10" height="10" rx="2" fill="{color}"/>')
        parts.append(f'<text x="{cx + 15}" y="{y + 3}" font-size="11" fill="#dfe7f0">{label}</text>')
        cx += 15 + 8 * len(label) + 22
    return "".join(parts)


def render(results: dict, out: Path) -> None:
    order = sorted(LABELS, key=lambda n: results[n]["rows"][1000], reverse=True)
    height = TOP + len(order) * GROUP_H + 46

    parts: list[str] = []
    # ── легенды ──
    parts.append(_legend(LEFT_X0, 6, [(f"{rows} строк", color) for rows, color in ROWS_SPEC]))
    parts.append('<text x="' + str(RIGHT_X0) + '" y="9" font-size="11" fill="#dfe7f0">/ping, req/s (без БД)</text>')

    # ── сетка ──
    parts.append(_gridlines(LEFT_X0, LEFT_X1, [100, 1000, 10000]))
    parts.append(_gridlines(RIGHT_X0, RIGHT_X1, [1000, 10000, 100000]))

    for i, name in enumerate(order):
        cy = TOP + i * GROUP_H + GROUP_H / 2
        parts.append(
            f'<text x="{LABEL_X}" y="{cy + 4}" text-anchor="end" font-size="12.5" '
            f'fill="#dfe7f0">{LABELS[name]}</text>'
        )
        # /rows: три полосы
        for r, (rows, color) in enumerate(ROWS_SPEC):
            value = results[name]["rows"][rows]
            bx = _log_x(value, 100, 31623, LEFT_X0, LEFT_X1)
            by = cy + (r - 1) * 14
            parts.append(
                f'<rect x="{LEFT_X0}" y="{by - BAR_H / 2:.1f}" width="{bx - LEFT_X0:.1f}" '
                f'height="{BAR_H}" rx="2" fill="{color}"/>'
            )
            parts.append(
                f'<text x="{bx + 6:.1f}" y="{by + 4}" font-size="11" font-weight="600" '
                f'fill="#eaf1f9">{_fmt(value)}</text>'
            )
        # /ping: одна полоса
        ping = results[name]["ping"]
        px = _log_x(ping, 1000, 100000, RIGHT_X0, RIGHT_X1)
        parts.append(
            f'<rect x="{RIGHT_X0}" y="{cy - BAR_H / 2:.1f}" width="{px - RIGHT_X0:.1f}" '
            f'height="{BAR_H}" rx="2" fill="{COLORS[name]}"/>'
        )
        parts.append(
            f'<text x="{px + 6:.1f}" y="{cy + 4}" font-size="11" font-weight="600" '
            f'fill="#eaf1f9">{_fmt(ping)}</text>'
        )

    svg = (
        f'<svg viewBox="0 0 {WIDTH} {height}" width="100%" xmlns="http://www.w3.org/2000/svg" '
        f'role="img">'
        f'<rect width="{WIDTH}" height="{height}" rx="12" fill="#0f141c"/>'
        + "".join(parts)
        + "</svg>"
    )

    html = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>Ferrox — бенчмарк 1/100/1000 строк</title>
<style>
  :root { --bg:#0b0e14; --text:#dfe7f0; --muted:#8ea0b5; }
  * { box-sizing:border-box; }
  body { margin:0; padding:34px 40px 44px; background:
      radial-gradient(1100px 500px at 15% -10%, #16233a 0%, transparent 60%),
      radial-gradient(900px 450px at 100% 0%, #241a33 0%, transparent 55%), var(--bg);
      color:var(--text); font-family:"Segoe UI",system-ui,-apple-system,Roboto,sans-serif; }
  h1 { margin:0 0 6px; font-size:22px; color:#eaf1f9; font-weight:700; letter-spacing:.2px; }
  .sub { color:var(--muted); font-size:13px; margin:0 0 22px; }
  .card { background:linear-gradient(180deg, rgba(21,29,42,.75), rgba(13,18,26,.75));
          border:1px solid #1e2836; border-radius:14px; padding:18px 22px;
          box-shadow:0 16px 40px rgba(0,0,0,.4); max-width:1460px; }
</style></head><body>
  <h1>Ferrox против Litestar и FastAPI — 1 / 100 / 1000 строк из PostgreSQL</h1>
  <p class="sub">ab -c 50 -k · granian · 1 воркер · лучший из 2 · таблица bench_rows (1000 строк) · логарифмическая шкала</p>
  <div class="card">__SVG__</div>
</body></html>"""

    html = html.replace("__SVG__", svg)
    out.write_text(html, encoding="utf-8")
    print(f"saved {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", nargs="?", help="JSON с результатами (иначе — прогнать бенч)")
    parser.add_argument("-o", "--out", default="bench_rows.html")
    args = parser.parse_args()
    results = load(args.results) if args.results else collect()
    render(results, Path(args.out))


if __name__ == "__main__":
    main()
