"""Пробник: точный предел параметров батча и COPY против multi-row INSERT.

Две вещи, нужные для решения «как писать пачками»:

  1. Точная граница параметров у asyncpg (клиентский лимит 32 767, а не 65 535
     серверный) — отсюда практический размер батча.
  2. `COPY` (`copy_records_to_table`) против multi-row `INSERT`: сколько строк/с
     даёт каждый путь при `synchronous_commit=on`.

Запуск: .venv/bin/python bench/_probe_copy.py
"""

from __future__ import annotations

import asyncio
import time

import asyncpg

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
CREATE = (
    "CREATE TABLE IF NOT EXISTS bench_batch ("
    "id serial PRIMARY KEY, name text NOT NULL, email text NOT NULL)"
)


async def probe_param_limit(conn) -> None:
    """Найти точку отказа по числу параметров (2 параметра на строку)."""
    for rows in (16_000, 16_300, 16_400, 17_000):
        params: list[str] = []
        tuples: list[str] = []
        for j in range(rows):
            base = len(params) + 1
            tuples.append(f"(${base}, ${base + 1})")
            params += [f"p{rows}_{j}", "e@e.com"]
        sql = f"INSERT INTO bench_batch (name, email) VALUES {', '.join(tuples)}"
        try:
            await conn.execute(sql, *params)
            print(f"параметры {len(params):>6} (строк {rows:>6}) — прошло")
        except Exception as exc:  # noqa: BLE001 - печатаем отказ
            print(f"параметры {len(params):>6} (строк {rows:>6}) — отказ ({type(exc).__name__}: {str(exc)[:70]})")
        await conn.execute("TRUNCATE bench_batch RESTART IDENTITY")


async def timed(label: str, rows: int, call) -> None:
    """Замерить запись ``rows`` строк за вызов; печатает строк/с."""
    start = time.perf_counter()
    await call()
    elapsed = time.perf_counter() - start
    print(f"{label:<34} {rows / elapsed:>10,.0f} строк/с   ({elapsed * 1e3:7.0f} мс всего)")


async def main() -> None:
    """Предел параметров, затем INSERT-батч против COPY."""
    conn = await asyncpg.connect(DSN)
    await conn.execute("DROP TABLE IF EXISTS bench_batch")
    await conn.execute(CREATE)

    await probe_param_limit(conn)

    for rows in (1_000, 10_000):
        # multi-row INSERT параметрами: безопасно до ~16 300 строк (лимит asyncpg)
        chunk = rows if rows <= 16_000 else 16_000
        params: list[str] = []
        tuples: list[str] = []
        for j in range(chunk):
            base = len(params) + 1
            tuples.append(f"(${base}, ${base + 1})")
            params += [f"i{rows}_{j}", "e@e.com"]
        sql = f"INSERT INTO bench_batch (name, email) VALUES {', '.join(tuples)}"
        await conn.execute("TRUNCATE bench_batch RESTART IDENTITY")
        await timed(f"INSERT-батч {rows} строк", rows, lambda: conn.execute(sql, *params))

        records = [(f"c{rows}_{j}", "e@e.com") for j in range(rows)]
        await conn.execute("TRUNCATE bench_batch RESTART IDENTITY")
        await timed(
            f"COPY {rows} строк",
            rows,
            lambda: conn.copy_records_to_table("bench_batch", records=records, columns=["name", "email"]),
        )

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
