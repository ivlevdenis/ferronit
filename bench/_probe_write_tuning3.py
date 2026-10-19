"""Пробник 3: остались ли рычаги у одиночной записи (50 клиентов).

Проверяет настройки, которые влияют именно на коммит под конкуренцией:

    baseline            wal_sync_method=fdatasync, commit_delay=0
    commit_delay=1000   коммит спит до 1 мс, собирая других коммиттеров (commit_siblings)
    open_datasync       синхронизация WAL через open(2) + fdatasync
    fsync               синхронизация WAL через fsync (как «честный» вариант)

Все глобальные правки откатываются в конце и проверяются через pg_settings.

Запуск: .venv/bin/python bench/_probe_write_tuning3.py
"""

from __future__ import annotations

import asyncio
import time

import asyncpg

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
CLIENTS = 50
PER_CLIENT = 40
CREATE = (
    "CREATE TABLE IF NOT EXISTS bench_rows ("
    "id serial PRIMARY KEY, name text NOT NULL, email text NOT NULL)"
)


async def seed(conn) -> None:
    """1000 строк, autovacuum off, чистый WAL."""
    await conn.execute(CREATE)
    await conn.execute("ALTER TABLE bench_rows SET (autovacuum_enabled = false)")
    await conn.execute("TRUNCATE bench_rows RESTART IDENTITY")
    await conn.execute(
        "INSERT INTO bench_rows (name, email) SELECT 'u' || g, 'u' || g || '@e.com' "
        "FROM generate_series(1, 1000) AS g"
    )


async def timed_parallel(label: str, pool, conn) -> float:
    """Одиночные INSERT из 50 соединений одновременно; печатает записей/с."""
    await seed(conn)
    await conn.execute("CHECKPOINT")
    await asyncio.sleep(3)

    async def worker(idx: int) -> None:
        c = await pool.acquire()
        try:
            for i in range(PER_CLIENT):
                await c.execute(
                    "INSERT INTO bench_rows (name, email) VALUES ($1, $2)",
                    f"n{idx}_{i}",
                    "e@e.com",
                )
        finally:
            await pool.release(c)

    start = time.perf_counter()
    await asyncio.gather(*[worker(j) for j in range(CLIENTS)])
    elapsed = time.perf_counter() - start
    total = CLIENTS * PER_CLIENT
    per_s = total / elapsed
    print(f"{label:<28} {per_s:>10,.0f} записей/с   ({elapsed * 1e6 / total:5.0f} мкс/запись)")
    return per_s


async def set_global(conn, name: str, value: str | None) -> None:
    """ALTER SYSTEM SET/RESET + reload."""
    if value is None:
        await conn.execute(f"ALTER SYSTEM RESET {name}")
    else:
        await conn.execute(f"ALTER SYSTEM SET {name} = '{value}'")
    await conn.execute("SELECT pg_reload_conf()")
    await asyncio.sleep(0.5)


async def show(conn) -> str:
    """Текущие значения интересных параметров."""
    rows = await conn.fetch(
        "SELECT name, setting FROM pg_settings WHERE name IN "
        "('wal_sync_method', 'commit_delay', 'commit_siblings', 'synchronous_commit')"
    )
    return ", ".join(f"{r['name']}={r['setting']}" for r in rows)


async def main() -> None:
    """Проверить остальные рычаги одиночной записи при 50 клиентах."""
    conn = await asyncpg.connect(DSN)
    vals = await conn.fetchval("SELECT enumvals FROM pg_settings WHERE name = 'wal_sync_method'")
    print(f"wal_sync_method варианты: {vals}")
    print(f"старт: {await show(conn)}")

    pool = await asyncpg.create_pool(DSN, min_size=CLIENTS, max_size=CLIENTS)

    await timed_parallel("baseline (fdatasync)", pool, conn)

    await set_global(conn, "commit_delay", "1000")
    await set_global(conn, "commit_siblings", "2")
    await timed_parallel("commit_delay=1000", pool, conn)
    await set_global(conn, "commit_delay", None)
    await set_global(conn, "commit_siblings", None)

    await set_global(conn, "wal_sync_method", "open_datasync")
    await timed_parallel("wal_sync_method=open_datas", pool, conn)
    await set_global(conn, "wal_sync_method", None)

    await set_global(conn, "wal_sync_method", "fsync")
    await timed_parallel("wal_sync_method=fsync", pool, conn)
    await set_global(conn, "wal_sync_method", None)

    await timed_parallel("baseline повтор", pool, conn)

    print(f"\nпосле отката: {await show(conn)}")
    await conn.execute("ALTER TABLE bench_rows SET (autovacuum_enabled = true)")
    await pool.close()
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
