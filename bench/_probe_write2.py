"""Пробник 2: почему DELETE/UPDATE падают после длинной серии записей.

Гипотеза: замедляют не слой и не сам оператор, а состояние, накопленное
предыдущими операциями матрицы (грязные страницы/индексы, отложенный
чекпоинт), при живом fsync=on. Мерит 2000 DELETE в четырёх состояниях.

Запуск: .venv/bin/python bench/_probe_write2.py
"""

from __future__ import annotations

import asyncio
import time

import asyncpg

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
N = 2000


async def reset(conn, rows: int) -> None:
    """Пересоздать таблицу: autovacuum off, TRUNCATE, ``rows`` строк."""
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS bench_rows (id serial PRIMARY KEY, "
        "name text NOT NULL, email text NOT NULL)"
    )
    await conn.execute("ALTER TABLE bench_rows SET (autovacuum_enabled = false)")
    await conn.execute("TRUNCATE bench_rows RESTART IDENTITY")
    await conn.execute(
        "INSERT INTO bench_rows (name, email) SELECT 'u' || g, 'u' || g || '@e.com' "
        "FROM generate_series(1, $1) AS g",
        rows,
    )


async def timed_delete(label: str, conn) -> float:
    """2000 DELETE по своим строкам после паузы; печатает запросов/с."""
    await reset(conn, 5000)
    await conn.execute("CHECKPOINT")
    await asyncio.sleep(10)          # дать диску устояться (снять остаточный I/O)
    start = time.perf_counter()
    for i in range(N):
        await conn.execute("DELETE FROM bench_rows WHERE id = $1", i + 1)
    per_s = N / (time.perf_counter() - start)
    print(f"{label:<44} {per_s:>9,.0f} запросов/с")
    return per_s


async def main() -> None:
    """Сравнить DELETE сразу после разных предысторий."""
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    conn = await pool.acquire()

    await timed_delete("DELETE на свежей таблице", conn)

    await reset(conn, 1000)
    for i in range(3000):
        await conn.execute(
            "INSERT INTO bench_rows (name, email) VALUES ($1, $2)", f"n{i}", "e@e.com"
        )
    await timed_delete("DELETE после 3000 INSERT", conn)

    await reset(conn, 1000)
    for i in range(3000):
        await conn.execute("UPDATE bench_rows SET name = $1 WHERE id = 1", f"n{i}")
    await timed_delete("DELETE после 3000 UPDATE", conn)

    await reset(conn, 1000)
    for i in range(3000):
        await conn.execute(
            "INSERT INTO bench_rows (id, name, email) VALUES (1, $1, $2) "
            "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name",
            f"n{i}",
            "e@e.com",
        )
    await timed_delete("DELETE после 3000 UPSERT", conn)

    await conn.execute("ALTER TABLE bench_rows SET (autovacuum_enabled = true)")
    await pool.release(conn)
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
