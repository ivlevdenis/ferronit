"""Пробник: можно ли разогнать запись в PostgreSQL — и чем именно.

Мерит один и тот же одиночный INSERT в `bench_rows` при разных настройках и
способах записи, записей/с (1 клиент, 1 соединение):

    baseline            synchronous_commit=on, fsync=on (как в проде)
    sync_commit_off     synchronous_commit=off (per-session, ничего не ломает глобально)
    fsync_off           fsync=off через ALTER SYSTEM + reload (возвращается назад)
    fpw_off             full_page_writes=off (fsync=on)
    batch_10            INSERT 10 строк одним оператором (1 транзакция)
    batch_100           INSERT 100 строк одним оператором
    unlogged_table      INSERT в UNLOGGED-таблицу (1 строка/транзакция)

Все глобальные правки откатываются в конце и проверяются через SHOW.

Запуск: .venv/bin/python bench/_probe_write_tuning.py
"""

from __future__ import annotations

import asyncio
import time

import asyncpg

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
N = 2000              # итераций на замер
BATCH_ROWS = 100      # сколько строк в базовом сиде
CREATE = (
    "CREATE TABLE IF NOT EXISTS bench_rows ("
    "id serial PRIMARY KEY, name text NOT NULL, email text NOT NULL)"
)
CREATE_UNLOGGED = (
    "CREATE UNLOGGED TABLE IF NOT EXISTS bench_rows_unlogged ("
    "id serial PRIMARY KEY, name text NOT NULL, email text NOT NULL)"
)


async def seed(conn, table: str = "bench_rows") -> None:
    """Пересоздать таблицу: 1000 строк, autovacuum off."""
    await conn.execute(CREATE)
    await conn.execute(CREATE_UNLOGGED)
    await conn.execute(f"ALTER TABLE {table} SET (autovacuum_enabled = false)")
    await conn.execute(f"TRUNCATE {table} RESTART IDENTITY")
    await conn.execute(
        f"INSERT INTO {table} (name, email) SELECT 'u' || g, 'u' || g || '@e.com' "
        "FROM generate_series(1, 1000) AS g"
    )


async def timed(label: str, conn, call, rows_per_iter: int = 1) -> float:
    """Прогнать ``N`` итераций записи; печатает записей/с (и мкс на итерацию)."""
    await conn.execute("CHECKPOINT")
    await asyncio.sleep(3)
    start = time.perf_counter()
    for i in range(N):
        await call(i)
    elapsed = time.perf_counter() - start
    per_s = N * rows_per_iter / elapsed
    print(f"{label:<22} {per_s:>10,.0f} записей/с   ({elapsed / N * 1e6:6.0f} мкс/итерация)")
    return per_s


async def main() -> None:
    """Сравнить настройки и способы записи на одном и том же INSERT."""
    conn = await asyncpg.connect(DSN)

    async def one(i: int) -> None:
        await conn.execute(
            "INSERT INTO bench_rows (name, email) VALUES ($1, $2)", f"n{i}", "e@e.com"
        )

    async def batch(i: int, rows: int):
        values = ", ".join(f"('n{i}_{j}', 'e@e.com')" for j in range(rows))
        await conn.execute(f"INSERT INTO bench_rows (name, email) VALUES {values}")

    print("настройки сейчас:", await settings(conn))

    await seed(conn)
    await timed("baseline (fsync on)", conn, one)
    await timed("baseline повтор", conn, one)

    await conn.execute("SET synchronous_commit = off")
    await seed(conn)
    await timed("synchronous_commit=off", conn, one)
    await conn.execute("SET synchronous_commit = on")

    await set_global(conn, "fsync", "off")
    await seed(conn)
    await timed("fsync=off", conn, one)
    await set_global(conn, "fsync", None)

    await set_global(conn, "full_page_writes", "off")
    await seed(conn)
    await timed("full_page_writes=off", conn, one)
    await set_global(conn, "full_page_writes", None)

    await seed(conn)
    await timed("batch 10 строк", conn, lambda i: batch(i, 10), rows_per_iter=10)
    await seed(conn)
    await timed("batch 100 строк", conn, lambda i: batch(i, 100), rows_per_iter=100)

    await seed(conn, "bench_rows_unlogged")
    await timed(
        "unlogged таблица",
        conn,
        lambda i: conn.execute(
            "INSERT INTO bench_rows_unlogged (name, email) VALUES ($1, $2)", f"n{i}", "e@e.com"
        ),
    )

    print("\nнастройки после отката:", await settings(conn))
    await conn.execute("ALTER TABLE bench_rows SET (autovacuum_enabled = true)")
    await conn.execute("ALTER TABLE bench_rows_unlogged SET (autovacuum_enabled = true)")
    await conn.close()


async def settings(conn) -> dict[str, str]:
    """Текущие значения интересующих параметров."""
    rows = await conn.fetch(
        "SELECT name, setting FROM pg_settings "
        "WHERE name IN ('fsync', 'synchronous_commit', 'full_page_writes', 'wal_compression')"
    )
    return dict((r["name"], r["setting"]) for r in rows)


async def set_global(conn, name: str, value: str | None) -> None:
    """ALTER SYSTEM SET/RESET + reload (для параметров уровня конфигурации)."""
    if value is None:
        await conn.execute(f"ALTER SYSTEM RESET {name}")
    else:
        await conn.execute(f"ALTER SYSTEM SET {name} = '{value}'")
    await conn.execute("SELECT pg_reload_conf()")


if __name__ == "__main__":
    asyncio.run(main())
