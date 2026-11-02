"""Диаграммы бенча: одна стадия (проверка) и сводная по всем стадиям.

Модуль рисует SVG без внешних зависимостей (только stdlib) и, если в системе
есть `inkscape`, дополнительно экспортирует PNG. Вызывается из
`bench/api_bench.py` после каждой стадии, поэтому диаграмма всегда соответствует
последнему прогону.

Базовые линии: `×1` — результат Ferronit в той же стадии (same session).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# (имя приложения в api_bench, подпись на диаграмме, цвет)
# Подпись = фреймворк + слой доступа (ORM или «raw SQL», если запросы ручные).
PALETTE: list[tuple[str, str, str]] = [
    ("ferronit", "Ferronit (rawmodel: SQL builder + model)", "#c0392b"),
    ("ferronit-pipeline", "Ferronit pipeline (+ aggregate → DTO)", "#e08276"),
    ("blacksheep", "BlackSheep (SQLAlchemy Core)", "#2c3e50"),
    ("litestar", "Litestar (SQLAlchemy Core)", "#2980b9"),
    ("fastapi", "FastAPI (SQLAlchemy ORM)", "#27ae60"),
    ("django", "Django (Django ORM)", "#7f8c8d"),
    ("rust-axum", "Axum (sqlx, raw SQL → structs)", "#d35400"),
    ("rust-actix", "Actix (sqlx, raw SQL → structs)", "#f39c12"),
    ("go-pgx", "Go pgx (raw SQL → structs)", "#16a085"),
    ("go-sqlx", "Go sqlx (raw SQL → structs)", "#48c9b0"),
    ("go-gorm", "Go GORM (ORM)", "#a6b1b3"),
    ("go-ent", "Go ent (generated ORM)", "#c8d0d2"),

]

FONT = "DejaVu Sans, Helvetica, Arial, sans-serif"

# Базовая линия всех измерений: конвейерный вариант Ferronit (полный DDD-путь).
BASELINE = "ferronit-pipeline"
BASELINE_LABEL = "Ferronit pipeline"

# ── Раскладка подписей (одно правило на все диаграммы) ────────────────────────
# Подписи идут СРАЗУ за баром: [бар] отступ «×N.NN» отступ «req/s».
# Отступы считаются от ширины текста, а не магическими пикселями, поэтому
# правило не зависит от масштаба шкалы и длины чисел.
LABEL_GAP = 6          # px от конца бара до коэффициента
RATIO_VALUE_GAP = 12   # px между коэффициентом и абсолютным значением
EDGE_PAD = 14          # px от последней подписи до края картинки
CHAR_WIDTH = 0.60      # средняя ширина символа в долях кегля (DejaVu Sans, цифры/`×`)


def text_width(text: str, size: float) -> float:
    """Оценка ширины строки в px (без рендеринга шрифта).

    Args:
        text: Строка подписи.
        size: Кегль в px.

    Returns:
        Ширина в px; погрешность в пределах пары процентов в большую сторону.
    """
    return len(text) * size * CHAR_WIDTH


def _require_fits(kind: str, label: str, x_end: float, width: float) -> None:
    """Проверяет, что подпись не выходит за правый край и не наезжает на бар.

    Args:
        kind: Что за подпись (для текста ошибки).
        label: Сама подпись.
        x_end: Координата конца подписи.
        width: Ширина картинки.

    Raises:
        RuntimeError: Если подпись обрезается краем картинки.
    """
    if x_end > width - 2:
        raise RuntimeError(
            f"подпись {kind} «{label}» не влезает: конец {x_end:.0f}px при ширине {width:.0f}px"
        )

# Заголовки стадий: имя стадии → что именно мерялось.
STAGE_TITLES = {
    "ping": "GET /ping — empty route",
    "jwt": "GET /me — JWT auth (HS256 verify)",
    "insert": "POST /rows — insert one row",
    "update": "PUT /rows/1 — update one row",
    "update-spread": "PUT /rows/any — update a spread row",
    "upsert-spread": "POST /rows/upsert-any — upsert a spread row",
    "upsert": "POST /rows/upsert — upsert one row",
    "delete": "DELETE /rows/oldest — delete one live row",
    "mem1": "Memory footprint (PSS), 1 worker (MB)",
    "mem10": "Memory footprint (PSS), 10 workers (MB)",
}


def stage_title(stage: str) -> str:
    """Человекочитаемый заголовок стадии.

    Args:
        stage: Имя стадии (`ping`, `rows20`, `delete`, …).

    Returns:
        Подпись для заголовка диаграммы.
    """
    if stage in STAGE_TITLES:
        return STAGE_TITLES[stage]
    if stage.startswith("rows"):
        return f"GET /rows?limit={stage[4:]} — read N rows"
    return stage


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _rps(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ")


def _ticks(x_max: float) -> list[float]:
    """Круглые деления оси ×: шаг 0,25/0,5/1,0, не больше пяти интервалов."""
    for step in (0.25, 0.5, 1.0, 2.0):
        count = int(round(x_max / step))
        if count <= 5:
            return [i * step for i in range(count + 1)]
    step = 2.0
    return [i * step for i in range(int(x_max / step) + 1)]


def _export_png(svg_path: Path) -> Path | None:
    """Экспортирует SVG в PNG (192 dpi), если в системе есть inkscape."""
    if shutil.which("inkscape") is None:
        return None
    png = svg_path.with_suffix(".png")
    subprocess.run(
        ["inkscape", str(svg_path), "--export-type=png", f"--export-filename={png}",
         "--export-dpi=192"],
        check=False, capture_output=True,
    )
    return png if png.exists() else None


def render_stage(
    stage: str,
    values: dict[str, float],
    out_dir: Path,
    *,
    subtitle: str = "same session, best of rounds",
    unit: str = "req/s",
    reference: tuple[str, float, float] | None = None,
) -> Path:
    """Рисует диаграмму одной стадии: 12 баров, `×1` — базовая линия Ferronit.

    Args:
        stage: Имя стадии (`ping`, `rows20`, `insert`, …).
        values: Результаты по приложениям (req/s или МБ для стадий памяти).
        out_dir: Каталог, куда писать `stage-<stage>.svg` и `.png`.
        subtitle: Строка-подпись под заголовком.

    Returns:
        Путь к SVG-файлу.
    """
    apps = [a for a in PALETTE if a[0] in values]
    base = values.get(BASELINE) or max(values.values())
    ratios = {a[0]: values[a[0]] / base for a in apps}
    top = max(ratios.values())
    x_max = max(2.0, round(top * 1.25, 1))

    bar_h, gap = 22, 8
    left, x0, plot_w = 330, 340, 390
    head, foot = 96, 80 if reference else 58
    height = head + len(apps) * (bar_h + gap) - gap + foot
    ratio_font, value_font = 11.5, 11

    def sx(ratio: float) -> float:
        return x0 + ratio / x_max * plot_w

    # Ширина = график + самые длинные подписи «×N.NN» и значение за самым длинным баром.
    labels_need = max(
        LABEL_GAP
        + text_width(f"×{ratios[a[0]]:.2f}", ratio_font)
        + RATIO_VALUE_GAP
        + text_width(_rps(values[a[0]]), value_font)
        + (10 + text_width("← baseline", 9.5) if a[0] == BASELINE else 0)
        for a in apps
    )
    hint = "lower is better — " if stage.startswith("mem") else ""
    footer = None
    if reference is not None:
        _name, _raw, _ratio = reference
        footer = (
            f"{_name}: {_rps(_raw)} {unit} → ×{_ratio:.2f} to baseline "
            f"· same statement, {50} clients, no framework layer"
        )
    subtitle = (
        f"Bars: × to {BASELINE_LABEL}=1.00 ({subtitle}) — dashed line = baseline; "
        f"after each bar: ratio, then {unit}" + (f"; {hint[:-3]}" if hint else "")
    )
    width = max(
        x0 + plot_w + 60,
        sx(max(ratios.values())) + labels_need + EDGE_PAD,
        44 + text_width(f"{stage_title(stage)}", 19) + 22,
        44 + text_width(subtitle, 12) + 22,
        44 + text_width(footer, 11) + 22 if footer else 0,
    )

    title = stage_title(stage)
    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="{FONT}">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text x="22" y="34" font-size="19" font-weight="700" fill="#111">'
        f'{_esc(title)}</text>',
        f'<text x="22" y="56" font-size="12" fill="#555">{_esc(subtitle)}</text>',
    ]

    for tick in _ticks(x_max):
        x = sx(tick)
        is_base = abs(tick - 1.0) < 1e-9
        colour = "#c0392b" if is_base else "#ececec"
        dash = ' stroke-dasharray="5 3"' if is_base else ""
        out.append(
            f'<line x1="{x:.1f}" y1="66" x2="{x:.1f}" y2="{height - foot + 4}" '
            f'stroke="{colour}" stroke-width="{1.6 if is_base else 1}"{dash}/>'
        )
        label = f"×{tick:g}"
        out.append(f'<text x="{x:.1f}" y="{height - foot + 20}" font-size="10.5" '
                   f'fill="#666" text-anchor="middle">{label}</text>')



    for i, (app, label, colour) in enumerate(apps):
        y = head + i * (bar_h + gap)
        mid = y + bar_h - 7
        ratio = ratios[app]
        is_ferronit = app == BASELINE
        out.append(f'<rect x="18" y="{y + 5}" width="10" height="10" rx="2" fill="{colour}"/>')
        weight = ' font-weight="700"' if is_ferronit else ""
        out.append(f'<text x="34" y="{mid}" font-size="12" fill="#2b2b2b"{weight}>'
                   f'{_esc(label)}</text>')

        end = sx(ratio)
        out.append(f'<rect x="{x0}" y="{y}" width="{max(end - x0, 1.5):.1f}" height="{bar_h}" '
                   f'rx="3" fill="{colour}"/>')
        fill = "#c0392b" if is_ferronit else "#333"
        ratio_text = f"×{ratio:.2f}"
        x_ratio = end + LABEL_GAP
        x_value = x_ratio + text_width(ratio_text, ratio_font) + RATIO_VALUE_GAP
        out.append(f'<text x="{x_ratio:.1f}" y="{mid}" font-size="{ratio_font}" '
                   f'fill="{fill}"{weight}>{ratio_text}</text>')
        value_text = _rps(values[app])
        out.append(f'<text x="{x_value:.1f}" y="{mid}" font-size="{value_font}" '
                   f'fill="#666">{value_text}</text>')
        x_after = x_value + text_width(value_text, value_font)
        if is_ferronit:
            out.append(f'<text x="{x_after + 10:.1f}" y="{mid}" font-size="9.5" '
                       f'fill="#c0392b">← baseline</text>')
            x_after += 10 + text_width("← baseline", 9.5)
        _require_fits("значение", value_text, x_after, width)

    if footer is not None:
        out.append(
            f'<text x="22" y="{height - 16}" font-size="11" fill="#111" '
            f'font-weight="600">{_esc(footer)}</text>'
        )

    out.append("</svg>")
    out_dir.mkdir(parents=True, exist_ok=True)
    svg = out_dir / f"stage-{stage}.svg"
    svg.write_text("\n".join(out), encoding="utf-8")
    _export_png(svg)
    return svg


def render_combined(
    results: dict[str, dict[str, float]],
    out_dir: Path,
    *,
    subtitle: str = "same session, best of 3 rounds",
) -> Path:
    """Рисует сводную диаграмму: все стадии × все приложения, `×1` — Ferronit.

    Args:
        results: Стадия → (приложение → req/s).
        out_dir: Каталог, куда писать `bench-ferronit-baseline.svg` и `.png`.
        subtitle: Строка-подпись под заголовком.

    Returns:
        Путь к SVG-файлу.
    """
    # Стадии без базовой линии (частичный прогон BENCH_APPS) в сводную не идут:
    # их отношения посчитать не от чего.
    stages = [s for s in results if results[s].get(BASELINE)]
    apps = [a for a in PALETTE if a[0] != "pgbench" and any(a[0] in results[s] for s in stages)]
    ratios = {
        s: {
            a[0]: results[s].get(a[0], 0.0) / (results[s].get(BASELINE) or max(results[s].values()))
            for a in apps
        }
        for s in stages
    }
    x_max = max(2.0, round(max(max(r.values()) for r in ratios.values()) * 1.2, 1))

    bar_h, bar_gap = 12, 2
    group_head, group_gap = 22, 14
    left, x0, plot_w = 330, 342, 380
    head, foot = 152, 60
    group_h = group_head + len(apps) * (bar_h + bar_gap) - bar_gap
    height = head + len(stages) * (group_h + group_gap) - group_gap + foot
    ratio_font, value_font = 9.5, 9.5

    def sx(ratio: float) -> float:
        return x0 + ratio / x_max * plot_w

    labels_need = max(
        LABEL_GAP
        + text_width(f"×{ratios[s][a[0]]:.2f}", ratio_font)
        + RATIO_VALUE_GAP
        + text_width(_rps(results[s][a[0]]), value_font)
        for s in stages
        for a in apps
        if a[0] in results[s]
    )
    width = max(
        x0 + plot_w + 60,
        sx(max(max(r.values()) for r in ratios.values())) + labels_need + EDGE_PAD,
    )

    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="{FONT}">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        '<text x="22" y="34" font-size="20" font-weight="700" fill="#111">'
        f'{BASELINE_LABEL} as baseline: requests per second</text>',
        f'<text x="22" y="56" font-size="12" fill="#555">Bars: × to {BASELINE_LABEL}=1.00 on '
        f'the same endpoint ({subtitle}); after each bar: ratio, then req/s</text>',
    ]
    for i, (_, label, colour) in enumerate(apps):
        col, row = i % 3, i // 3
        lx, ly = 22 + col * 290, 78 + row * 16
        out.append(f'<rect x="{lx}" y="{ly - 8}" width="9" height="9" rx="2" fill="{colour}"/>')
        out.append(f'<text x="{lx + 13}" y="{ly}" font-size="10.5" fill="#333">'
                   f'{_esc(label)}</text>')

    for tick in _ticks(x_max):
        x = sx(tick)
        is_base = abs(tick - 1.0) < 1e-9
        colour = "#c0392b" if is_base else "#ececec"
        dash = ' stroke-dasharray="5 3"' if is_base else ""
        out.append(f'<line x1="{x:.1f}" y1="{head - 12}" x2="{x:.1f}" '
                   f'y2="{height - foot + 4}" stroke="{colour}" '
                   f'stroke-width="{1.6 if is_base else 1}"{dash}/>')
        out.append(f'<text x="{x:.1f}" y="{height - foot + 20}" font-size="10.5" fill="#666" '
                   f'text-anchor="middle">×{tick:g}</text>')
    for gi, stage in enumerate(stages):
        top = head + gi * (group_h + group_gap)
        out.append(f'<text x="22" y="{top + 1}" font-size="12.5" font-weight="700" '
                   f'fill="#111">{_esc(stage_title(stage))}</text>')
        best = max(ratios[stage][a[0]] for a in apps if a[0] != BASELINE) if len(apps) > 1 else 1
        for ai, (app, label, colour) in enumerate(apps):
            y = top + group_head + ai * (bar_h + bar_gap)
            mid = y + bar_h - 3
            ratio = ratios[stage][app]
            is_ferronit = app == BASELINE
            out.append(f'<rect x="22" y="{y + 2}" width="7" height="7" rx="1.5" fill="{colour}"/>')
            weight = ' font-weight="700"' if is_ferronit else ""
            out.append(f'<text x="34" y="{mid}" font-size="10" fill="#2b2b2b"{weight}>'
                       f'{_esc(label)}</text>')
            end = sx(ratio)
            out.append(f'<rect x="{x0}" y="{y}" width="{max(end - x0, 1.2):.1f}" '
                       f'height="{bar_h}" rx="2" fill="{colour}"/>')
            fill = "#c0392b" if is_ferronit else "#333"
            bold = ' font-weight="700"' if abs(ratio - best) < 1e-9 else ""
            ratio_text = f"×{ratio:.2f}"
            x_ratio = end + LABEL_GAP
            x_value = x_ratio + text_width(ratio_text, ratio_font) + RATIO_VALUE_GAP
            out.append(f'<text x="{x_ratio:.1f}" y="{mid}" font-size="{ratio_font}" '
                       f'fill="{fill}"{weight or bold}>{ratio_text}</text>')
            value_text = _rps(results[stage][app])
            out.append(f'<text x="{x_value:.1f}" y="{mid}" font-size="{value_font}" '
                       f'fill="#666">{value_text}</text>')
            _require_fits(
                "значение", value_text, x_value + text_width(value_text, value_font), width
            )

    out.append("</svg>")
    out_dir.mkdir(parents=True, exist_ok=True)
    svg = out_dir / "bench-ferronit-baseline.svg"
    svg.write_text("\n".join(out), encoding="utf-8")
    _export_png(svg)
    return svg


def main(argv: list[str] | None = None) -> None:
    """Перерисовать диаграммы из ``bench/results/*.json`` без повторного прогона.

    Запуск:
        ``.venv/bin/python bench/chart.py``                — обновить диаграммы в docs/img/charts
        ``.venv/bin/python bench/chart.py --check``        — проверить раскладку, ничего не записывая
        ``.venv/bin/python bench/chart.py <каталог стенда>`` — взять JSON из другого каталога
    """
    import json
    import sys
    import tempfile

    args = list(sys.argv[1:] if argv is None else argv)
    check_only = "--check" in args
    args = [a for a in args if a != "--check"]
    root = Path(args[0]) if args else Path(__file__).resolve().parent
    results_dir = root / "results"
    charts_dir = (
        Path(tempfile.mkdtemp(prefix="bench-charts-"))
        if check_only
        else root.parent / "docs" / "img" / "charts"
    )

    speed: dict[str, dict[str, float]] = {}
    for path in sorted(results_dir.glob("stage-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        stage = payload["stage"]
        values = payload["values"]
        runs = payload.get("runs", 3)
        unit = "МБ" if stage.startswith("mem") else "req/s"
        reference = None
        tps = payload.get("pgbench_tps")
        if tps and BASELINE in values:
            reference = ("pgbench (DB ceiling)", tps, tps / values[BASELINE])
        svg = render_stage(
            stage, values, charts_dir, subtitle=f"best of {runs} rounds", unit=unit,
            reference=reference,
        )
        print(f"{stage:10s} → {svg}")
        if not stage.startswith("mem"):
            speed[stage] = values

    order = ["ping", "rows1", "rows20", "rows50", "rows100", "rows1000",
             "insert", "update", "upsert", "delete"]
    speed = {k: speed[k] for k in order if k in speed} | {
        k: v for k, v in speed.items() if k not in order
    }
    if speed:
        svg = render_combined(speed, charts_dir, subtitle="same session, best of rounds")
        print(f"combined   → {svg}")

    if check_only:
        print(f"layout ok: {len(speed) + 1} диаграмм, подписи справа от баров, без обрезки")


if __name__ == "__main__":
    main()
