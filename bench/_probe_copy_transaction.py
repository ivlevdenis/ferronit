"""Пробник: участвует ли COPY в транзакции (критично для Unit of Work).

Первый прогон показал, что `copy_records_to_table` внутри `async with conn.transaction()`
не откатился. Проверяю прицельно, с контролем на обычном INSERT:

    a) INSERT в транзакции + ROLLBACK            — контроль (должен откатиться);
    b) COPY в `async with conn.transaction()` + откат;
    c) COPY между явными BEGIN/ROLLBACK;
    d) COPY + ошибка ПОСЛЕ (исключение внутри блока) — откатились ли строки.

Запуск: .venv/bin/python bench/_probe_copy_transaction.py
"""

from __future__ import annotations

import asyncio
import inspect

import asyncpg

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
CREATE = "CREATE TABLE IF NOT EXISTS copy_tx (id serial PRIMARY KEY, name text NOT NULL)"
RECORDS = [(f"q{i}",) for i in range(10)]


async def count(conn) -> int:
    """Число строк в таблице."""
    return await conn.fetchval("SELECT count(*) FROM copy_tx")


async def main() -> None:
    """Четыре сценария отката COPY и обычного INSERT."""
    conn = await asyncpg.connect(DSN)
    await conn.execute(CREATE)

    sig = inspect.signature(conn.copy_records_to_table)
    print(f"copy_records_to_table: {sig}")

    await conn.execute("TRUNCATE copy_tx")
    async with conn.transaction():
        await conn.execute("INSERT INTO copy_tx (name) VALUES ('ins')")
        raise_here = False
    print(f"a) контроль INSERT в transaction() (без исключения, коммит): {await count(conn)}")

    await conn.execute("TRUNCATE copy_tx")
    try:
        async with conn.transaction():
            await conn.execute("INSERT INTO copy_tx (name) VALUES ('ins')")
            raise RuntimeError("откат")
    except RuntimeError:
        pass
    print(f"b) контроль INSERT + исключение (должен откатиться): {await count(conn)}")

    await conn.execute("TRUNCATE copy_tx")
    try:
        async with conn.transaction():
            await conn.copy_records_to_table("copy_tx", records=RECORDS, columns=["name"])
            raise RuntimeError("откат")
    except RuntimeError:
        pass
    print(f"c) COPY + исключение в transaction(): {await count(conn)} (0 = откатился)")

    await conn.execute("TRUNCATE copy_tx")
    await conn.execute("BEGIN")
    await conn.copy_records_to_table("copy_tx", records=RECORDS, columns=["name"])
    await conn.execute("ROLLBACK")
    print(f"d) COPY между явными BEGIN/ROLLBACK: {await count(conn)} (0 = откатился)")

    await conn.execute("TRUNCATE copy_tx")
    await conn.execute("BEGIN")
    await conn.execute("INSERT INTO copy_tx (name) VALUES ('ins')")
    await conn.copy_records_to_table("copy_tx", records=RECORDS, columns=["name"])
    await conn.execute("COMMIT")
    print(f"e) INSERT + COPY в одном BEGIN/COMMIT: {await count(conn)} (11 = обе операции вместе)")

    await conn.execute("TRUNCATE copy_tx")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
