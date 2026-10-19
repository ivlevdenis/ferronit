"""Пробник: сколько даёт диск на одиночных записях (вне стенда сравнения).

Отвечает на вопрос «почему в стенде падают upsert/delete»: если простой
asyncpg UPDATE/DELETE здесь даёт те же ~330 запросов/с, то мерилась задержка
диска (fsync), а не слой. Мерит три сценария подряд и печатает запросов/с.

Запуск: .venv/bin/python bench/_probe_write.py
"""

from __future__ import annotations

import asyncio
import time

import asyncpg

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
N = 500


async def timed(label: str, call) -> None:
    """Прогнать ``N`` вызовов ``call`` и напечатать запросов/с."""
    start = time.perf_counter()
    for i in range(N):
        await call(i)
    elapsed = time.perf_counter() - start
    print(f"{label:<28} {N / elapsed:>10,.0f} запросов/с   ({elapsed / N * 1e6:6.0f} мкс)")


async def main() -> None:
    """Сравнить UPDATE/DELETE по одному соединению из пула."""
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=16)
    conn = await pool.acquire()

    await conn.execute("CREATE TABLE IF NOT EXISTS bench_rows (id serial PRIMARY KEY, name text NOT NULL, email text NOT NULL)")
    await conn.execute("TRUNCATE bench_rows RESTART IDENTITY")
    await conn.execute(
        "INSERT INTO bench_rows (name, email) SELECT 'u' || g, 'u' || g || '@e.com' "
        "FROM generate_series(1, 5000) AS g"
    )

    await timed(
        "UPDATE id=1 (asyncpg)",
        lambda i: conn.execute(
            "UPDATE bench_rows SET name = $1 WHERE id = 1", f"n{i}"
        ),
    )
    await timed(
        "UPDATE id=1 (BEGIN/COMMIT)",
        lambda i: update_in_tx(conn, i),
    )
    await timed(
        "DELETE своей строки",
        lambda i: conn.execute("DELETE FROM bench_rows WHERE id = $1", i + 1),
    )

    await pool.release(conn)
    await pool.close()


async def update_in_tx(conn, i: int) -> None:
    """UPDATE внутри явной транзакции (как делает UoW/сессия)."""
    async with conn.transaction():
        await conn.execute("UPDATE bench_rows SET name = $1 WHERE id = 1", f"t{i}")


if __name__ == "__main__":
    asyncio.run(main())
