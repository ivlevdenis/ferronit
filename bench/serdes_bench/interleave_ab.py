"""Interleaved A/B: сборка ответа фреймворком против «сериализацию делает база».

Зачем отдельно от ``run_ab.sh``: тот гоняет варианты последовательно (сначала все
прогоны одного, потом другого), поэтому дрейф машины и базы попадает в разницу. Здесь
раунды чередуют варианты — каждый раунд меряет все три в одинаковых условиях, а в
отчёт идёт медиана по раундам.

Запуск: .venv/bin/python bench/serdes_bench/interleave_ab.py
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from cpu_bench import dsn_from_bench, run_ab, seed, start, wait_ready  # noqa: E402

VARIANTS = (
    (8351, "serdes_bench.app_framework:app", "framework"),
    (8354, "serdes_bench.app_cached:app", "cached"),
    (8355, "serdes_bench.app_jsonsql:app", "jsonsql"),
)
ENDPOINTS = (("/rows?limit=100", 10_000), ("/rows?limit=1000", 5_000))
ROUNDS = 5
RUNS_PER_ROUND = 3


def main() -> None:
    """Меряет варианты по раундам и печатает medians."""
    dsn = dsn_from_bench()
    seed(dsn)
    procs = []
    for port, app, _label in VARIANTS:
        procs.append((port, _label, start(port, app, dsn)))
    try:
        for port, _label, _proc in procs:
            wait_ready(port)
        for path, n in ENDPOINTS:
            for port, _label, _proc in procs:
                run_ab(port, path, 1000)  # прогрев
            results: dict[str, list[float]] = {label: [] for _p, label, _pr in procs}
            for _round in range(ROUNDS):
                for port, label, _proc in procs:
                    best = max(run_ab(port, path, n)[0] for _ in range(RUNS_PER_ROUND))
                    results[label].append(best)
            print(f"\n{path} — медиана по {ROUNDS} раундам (в раунде лучшее из "
                  f"{RUNS_PER_ROUND} прогонов), ab -c 50 -k")
            base = statistics.median(results["framework"])
            for label, values in results.items():
                med = statistics.median(values)
                print(f"  {label:<11} {med:9,.0f} req/s  ×{med / base:4.2f}  "
                      f"(мин {min(values):,.0f}, макс {max(values):,.0f})")
    finally:
        for _port, _label, proc in procs:
            proc.terminate()
            proc.wait()
    print(f"\nload в конце: {time.strftime('%H:%M')}")


if __name__ == "__main__":
    main()
