"""Пробник 2: воспроизводимость рычагов записи + групповой коммит.

Дополняет `_probe_write_tuning.py`: повторные замеры (вперемешку) для самых
эффективных режимов и кейс конкурентности — 50 соединений, одиночные INSERT,
где PostgreSQL объединяет fsync разных транзакций в один (group commit).

Запуск: .venv/bin/python bench/_probe_write_tuning2.py
"""

from __future__ import annotations

import asyncio
import time

import asyncpg

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
N = 2000
CLIENTS = 50
CREATE = (
    "CREATE TABLE IF NOT EXISTS bench_rows ("
    "id serial PRIMARY KEY, name text NOT NULL, email text NOT NULL)"
)


async def seed(conn) -> None:
    """Пересоздать таблицу: 1000 строк, autovacuum off."""
    await conn.execute(CREATE)
    await conn.execute("ALTER TABLE bench_rows SET (autovacuum_enabled = false)")
    await conn.execute("TRUNCATE bench_rows RESTART IDENTITY")
    await conn.execute(
        "INSERT INTO bench_rows (name, email) SELECT 'u' || g, 'u' || g || '@e.com' "
        "FROM generate_series(1, 1000) AS g"
    )


async def insert_one(conn, i: int) -> None:
    """Одиночный INSERT одной строки."""
    await conn.execute(
        "INSERT INTO bench_rows (name, email) VALUES ($1, $2)", f"n{i}", "e@e.com"
    )


async def timed(label: str, conn, call, rows_per_iter: int = 1, n: int = N) -> float:
    """Прогнать ``n`` итераций записи; печатает записей/с."""
    await conn.execute("CHECKPOINT")
    await asyncio.sleep(2)
    start = time.perf_counter()
    for i in range(n):
        await call(i)
    elapsed = time.perf_counter() - start
    per_s = n * rows_per_iter / elapsed
    print(f"{label:<34} {per_s:>10,.0f} записей/с   ({elapsed / n * 1e6:5.0f} мкс/итерация)")
    return per_s


async def timed_parallel(label: str, pool, n_total: int = N) -> float:
    """Одиночные INSERT из ``CLIENTS`` соединений одновременно (group commit)."""
    await asyncio.sleep(2)
    per_client = n_total // CLIENTS

    async def worker(idx: int) -> None:
        conn = await pool.acquire()
        try:
            for i in range(per_client):
                await insert_one(conn, idx * 1000 + i)
        finally:
            await pool.release(conn)

    start = time.perf_counter()
    await asyncio.gather(*[worker(j) for j in range(CLIENTS)])
    elapsed = time.perf_counter() - start
    per_s = CLIENTS * per_client / elapsed
    print(f"{label:<34} {per_s:>10,.0f} записей/с   ({elapsed * 1e6 / (CLIENTS * per_client):5.0f} мкс/итерация)")
    return per_s


async def main() -> None:
    """Повторить лучшие режимы вперемешку и замерить групповой коммит."""
    conn = await asyncpg.connect(DSN)
    pool = await asyncpg.create_pool(DSN, min_size=CLIENTS, max_size=CLIENTS)

    # A/B вперемешку: baseline, sync_commit=off, fsync=off — по 2 цикла.
    for cycle in (1, 2):
        await seed(conn)
        await conn.execute("SET synchronous_commit = on")
        await timed(f"цикл {cycle}: baseline (fsync on)", conn, lambda i: insert_one(conn, i))

        await seed(conn)
        await conn.execute("SET synchronous_commit = off")
        await timed(f"цикл {cycle}: synchronous_commit=off", conn, lambda i: insert_one(conn, i))
        await conn.execute("SET synchronous_commit = on")

    # Групповой коммит: 50 соединений, одиночные INSERT.
    await seed(conn)
    await timed_parallel("50 клиентов, fsync on", pool)

    # 50 клиентов с выключенным ожиданием WAL (настройка — на каждом соединении пула)
    await seed(conn)
    await pool.close()

    async def sync_off(connection) -> None:
        await connection.execute("SET synchronous_commit = off")

    pool = await asyncpg.create_pool(
        DSN, min_size=CLIENTS, max_size=CLIENTS, init=sync_off
    )
    await timed_parallel("50 клиентов, sync_commit=off", pool)

    # Комбо: батч 100 строк + synchronous_commit=off.
    await seed(conn)
    await conn.execute("SET synchronous_commit = off")

    async def batch(i: int) -> None:
        values = ", ".join(f"('b{i}_{j}', 'e@e.com')" for j in range(100))
        await conn.execute(f"INSERT INTO bench_rows (name, email) VALUES {values}")

    await timed("batch 100 + sync_commit=off", conn, batch, rows_per_iter=100)
    await conn.execute("SET synchronous_commit = on")

    await conn.execute("ALTER TABLE bench_rows SET (autovacuum_enabled = true)")
    await pool.close()
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
