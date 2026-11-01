"""Цена декодирования datetime в asyncpg: только даты / без дат / все колонки.

Показывает, какая доля стоимости строки приходится на декодирование
``timestamptz``/``date``/``interval``. Три набора колонок одного и того же
``SELECT ... LIMIT N``:

    datetime -- created_at, birthday, ttl   (только даты/время/интервал)
    plain    -- id, name, email             (без дат: int + 2 text)
    all      -- все шесть колонок

Разница между прогонами на разных сборках asyncpg для набора ``datetime`` —
это и есть реальный эффект изменения datetime-кодека; ``plain`` служит
контролем (там эффекта быть не должно), ``all`` — как это размывается
остальными колонками.

Запуск (текущая установка asyncpg):
    BENCH_DSN=... .venv/bin/python bench/datetime_decode_bench.py
Другая сборка asyncpg — подставить её каталог в PYTHONPATH:
    PYTHONPATH=/tmp/stock-sp .venv/bin/python bench/datetime_decode_bench.py
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime

from queries_vs_sqlalchemy import DSN, seed

LIMIT = int(os.environ.get("BENCH_LIST_LIMIT", "1000"))
ITERS = int(os.environ.get("BENCH_ITERS", "300"))
RUNS = int(os.environ.get("BENCH_RUNS", "3"))

COLSETS = {
    "datetime": "created_at, birthday, ttl",
    "plain": "id, name, email",
    "all": "id, name, email, created_at, birthday, ttl",
}


async def measure(pool, cols: str, iters: int) -> dict[str, float]:
    """Прогнать ``iters`` запросов с набором колонок ``cols``."""
    sql = f"SELECT {cols} FROM bench_rows LIMIT $1"
    cpu_start = time.process_time()
    start = time.perf_counter()
    for _ in range(iters):
        async with pool.acquire() as conn:
            await conn.fetch(sql, LIMIT)
    elapsed = time.perf_counter() - start
    cpu = time.process_time() - cpu_start
    return {"req_s": iters / elapsed, "cpu_us": cpu / iters * 1e6}


async def main() -> None:
    """Замерить три набора колонок и напечатать таблицу."""
    import asyncpg

    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=16)
    await seed(max(LIMIT, 1000))

    for cols in COLSETS.values():            # прогрев
        async with pool.acquire() as conn:
            await conn.fetch(f"SELECT {cols} FROM bench_rows LIMIT $1", LIMIT)

    best: dict[str, dict[str, float]] = {}
    for _ in range(RUNS):
        for name, cols in COLSETS.items():
            value = await measure(pool, cols, ITERS)
            prev = best.get(name)
            if prev is None or value["req_s"] > prev["req_s"]:
                best[name] = value

    print(
        f"# asyncpg {asyncpg.__version__} | LIMIT {LIMIT} | {datetime.now():%H:%M} | "
        f"load {os.getloadavg()[0]:.2f} | лучшее из {RUNS}, итераций {ITERS}"
    )
    print(f"{'колонки':<12}{'req/s':>12}{'CPU/запрос, мкс':>18}{'CPU/строка, мкс':>18}")
    for name in ("datetime", "plain", "all"):
        v = best[name]
        print(
            f"{name:<12}{v['req_s']:>12,.0f}{v['cpu_us']:>18,.0f}"
            f"{v['cpu_us'] / LIMIT:>18.3f}"
        )
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
