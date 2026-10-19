"""Пробник: практический размер батча — сколько строк влезает в параметризованный INSERT.

Склейка литералов (как в пробниках скорости) лимит не показывает: у протокола
PostgreSQL ограничение на 65 535 параметров на запрос, а не на длину SQL. Здесь
INSERT идёт параметрами ($1, $2, $3 на строку) — то есть так, как это делается
в приложении.

Запуск: .venv/bin/python bench/_probe_batch_limits.py
"""

from __future__ import annotations

import asyncio

import asyncpg

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
CREATE = (
    "CREATE TABLE IF NOT EXISTS bench_batch ("
    "id serial PRIMARY KEY, name text NOT NULL, email text NOT NULL)"
)


async def try_batch(conn, rows: int) -> None:
    """Параметризованный батч на ``rows`` строк: печатает параметры и результат."""
    params: list[str] = []
    tuples: list[str] = []
    for j in range(rows):
        base = len(params) + 1
        tuples.append(f"(${base}, ${base + 1})")
        params += [f"u{rows}_{j}", "e@e.com"]
    sql = f"INSERT INTO bench_batch (name, email) VALUES {', '.join(tuples)}"
    try:
        await conn.execute(sql, *params)
        print(f"строк {rows:>6}, параметров {len(params):>6} — прошло")
    except Exception as exc:  # noqa: BLE001 - показываем любой отказ
        print(f"строк {rows:>6}, параметров {len(params):>6} — отказ ({type(exc).__name__}: {str(exc)[:80]})")
    await conn.execute("TRUNCATE bench_batch RESTART IDENTITY")


async def main() -> None:
    """Проверить размеры батча вокруг лимита параметров протокола."""
    conn = await asyncpg.connect(DSN)
    await conn.execute("DROP TABLE IF EXISTS bench_batch")
    await conn.execute(CREATE)
    for rows in (1_000, 20_000, 32_000, 33_000, 40_000):
        await try_batch(conn, rows)
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
