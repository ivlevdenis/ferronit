"""Сколько бизнес-логика съедает от пропускной способности Ferronit на PostgreSQL.

Тот же протокол, что у bench_postgres.py (`ab -c 50 -k`), но маршрут сверх запроса
к базе ещё «жжёт» заданное число микросекунд CPU. Сравнение на 1 и 8 воркерах
показывает, где кончается GIL и начинается масштабирование процессами.

Запуск: .venv/bin/python bench/bench_logic.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV_PY = HERE.parent / ".venv" / "bin" / "python"
APP_MODULE = "_bench_logic_app"
PORT = 8211


def ab_reqs(path: str) -> float:
    out = subprocess.run(
        ["ab", "-n", "2000", "-c", "50", "-k", f"http://127.0.0.1:{PORT}{path}"],
        capture_output=True,
        text=True,
        timeout=300,
    ).stdout
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


def main() -> None:
    costs = (0, 100, 500, 1000)
    print(f"{'логика, мкс':>11s} {'1 воркер':>10s} {'8 воркеров':>10s}")
    results: dict[int, dict[int, float]] = {1: {}, 8: {}}
    for workers in (1, 8):
        proc = serve(workers)
        time.sleep(2.5)
        try:
            for us in costs:
                rps = ab_reqs(f"/work/{us}")
                results[workers][us] = rps
        finally:
            proc.terminate()
            proc.wait()
            time.sleep(1)
    for us in costs:
        print(f"{us:>11d} {results[1][us]:>10,.0f} {results[8][us]:>10,.0f}")


if __name__ == "__main__":
    main()
