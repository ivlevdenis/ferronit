"""Пробник: много клиентов — как разводятся транзакции и что бывает без блокировок.

Три опыта на одном пуле соединений (как в приложении, где на каждый запрос берётся
свой Unit of Work):

    A. Изоляция: 50 клиентов, каждый в своей транзакции делает 2 вставки с паузой.
       Проверяем, что чужие незакоммиченные строки не видны, а после коммита строк
       ровно 2 × клиентов.
    B. Откат: из 50 транзакций 5 падают в середине. Их строки исчезают, чужие целы.
    C. Гонка счётчика: 50 клиентов × 40 инкрементов (2000 всего) тремя способами —
       read-modify-write в Python, атомарный `UPDATE ... v = v + 1`, `SELECT ... FOR UPDATE`.
       Показывает, что «развести транзакции» мало: при конкурентной записи нужна
       блокировка или атомарная операция.

Запуск: .venv/bin/python bench/_probe_tx_isolation.py
"""

from __future__ import annotations

import asyncio
import time

import asyncpg

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
CLIENTS = 50
INCR_PER_CLIENT = 40
DDL = """
CREATE TABLE IF NOT EXISTS tx_demo (
    id serial PRIMARY KEY, client int NOT NULL, marker text NOT NULL);
CREATE TABLE IF NOT EXISTS tx_counters (id int PRIMARY KEY, v int NOT NULL);
"""


async def reset(pool) -> None:
    """Пересоздать таблицы опыта (одна транзакция юнита работы)."""
    async with pool.uow() as uow:
        conn = uow.connection
        await conn.execute(DDL)
        await conn.execute("TRUNCATE tx_demo RESTART IDENTITY")
        await conn.execute("TRUNCATE tx_counters")
        await conn.execute("INSERT INTO tx_counters (id, v) VALUES (1, 0)")


async def worker_isolation(pool, client: int) -> tuple[int, int]:
    """Транзакция клиента: 2 вставки + проверка, что чужие строки не видны."""
    from ferronit.contrib.rawdb import Pool as RawPool

    assert isinstance(pool, RawPool)
    async with pool.uow() as uow:
        repo = uow["tx_demo"]
        await repo.save({"client": client, "marker": f"c{client}"})
        await asyncio.sleep(0.005)          # транзакция «думает» — окно для конфликта
        await repo.save({"client": client, "marker": f"c{client}"})
        mine = await uow.connection.fetchval(
            "SELECT count(*) FROM tx_demo WHERE marker = $1", f"c{client}"
        )
        others = await uow.connection.fetchval(
            "SELECT count(*) FROM tx_demo WHERE marker <> $1", f"c{client}"
        )
    return mine, others


async def worker_rollback(pool, client: int, fail: bool) -> None:
    """Транзакция клиента, которая может упасть в середине (проверка отката)."""
    from ferronit.contrib.rawdb import Pool as RawPool

    assert isinstance(pool, RawPool)
    async with pool.uow() as uow:
        repo = uow["tx_demo"]
        await repo.save({"client": client, "marker": f"r{client}"})
        if fail:
            raise RuntimeError("клиент отвалился в середине транзакции")
        await repo.save({"client": client, "marker": f"r{client}"})


async def rw_increment(pool, client: int) -> None:
    """Плохой путь: прочитал значение в Python, записал обратно (гонка)."""
    from ferronit.contrib.rawdb import Pool as RawPool

    assert isinstance(pool, RawPool)
    for _ in range(INCR_PER_CLIENT):
        async with pool.uow() as uow:
            value = await uow.connection.fetchval("SELECT v FROM tx_counters WHERE id = 1")
            await asyncio.sleep(0.0002)
            await uow.connection.execute(
                "UPDATE tx_counters SET v = $1 WHERE id = 1", value + 1
            )


async def atomic_increment(pool, client: int) -> None:
    """Хороший путь: инкремент делает сама база — блокировка строки не нужна в коде."""
    from ferronit.contrib.rawdb import Pool as RawPool

    assert isinstance(pool, RawPool)
    for _ in range(INCR_PER_CLIENT):
        async with pool.uow() as uow:
            await uow.connection.execute("UPDATE tx_counters SET v = v + 1 WHERE id = 1")


async def locked_increment(pool, client: int) -> None:
    """Путь с явной блокировкой: `SELECT ... FOR UPDATE` держит строку до коммита."""
    from ferronit.contrib.rawdb import Pool as RawPool

    assert isinstance(pool, RawPool)
    for _ in range(INCR_PER_CLIENT):
        async with pool.uow() as uow:
            value = await uow.connection.fetchval(
                "SELECT v FROM tx_counters WHERE id = 1 FOR UPDATE"
            )
            await asyncio.sleep(0.0002)
            await uow.connection.execute(
                "UPDATE tx_counters SET v = $1 WHERE id = 1", value + 1
            )


async def main() -> None:
    """Прогнать три опыта на пуле в 50 соединений."""
    from ferronit.contrib.rawdb import create_pool

    pool = await create_pool(DSN, min_size=CLIENTS, max_size=CLIENTS)

    await reset(pool)
    results = await asyncio.gather(*[worker_isolation(pool, c) for c in range(CLIENTS)])
    async with pool.uow(readonly=True) as uow:
        total = await uow.connection.fetchval("SELECT count(*) FROM tx_demo")
    print(
        f"A. изоляция: клиентов {CLIENTS}, у каждого внутри транзакции своих строк "
        f"{sorted({m for m, _ in results})}, чужих {sorted({o for _, o in results})}, "
        f"в таблице после всех коммитов {total}"
    )

    await reset(pool)
    failing = {3, 17, 25, 30, 41}
    await asyncio.gather(
        *[worker_rollback(pool, c, c in failing) for c in range(CLIENTS)],
        return_exceptions=True,
    )
    async with pool.uow(readonly=True) as uow:
        total = await uow.connection.fetchval("SELECT count(*) FROM tx_demo")
    print(
        f"B. откат: упало транзакций {len(failing)}, строк в таблице {total} "
        f"(ожидалось 2 × {CLIENTS - len(failing)} = {2 * (CLIENTS - len(failing))})"
    )

    for label, worker in (
        ("read-modify-write (гонка)", rw_increment),
        ("атомарный UPDATE v = v + 1", atomic_increment),
        ("SELECT ... FOR UPDATE", locked_increment),
    ):
        await reset(pool)
        start = time.perf_counter()
        await asyncio.gather(*[worker(pool, c) for c in range(CLIENTS)])
        elapsed = time.perf_counter() - start
        async with pool.uow(readonly=True) as uow:
            value = await uow.connection.fetchval("SELECT v FROM tx_counters WHERE id = 1")
        expected = CLIENTS * INCR_PER_CLIENT
        print(
            f"C. {label:<28} счётчик {value:>5} из {expected}, потеряно {expected - value:>5}, "
            f"{elapsed:5.2f} с"
        )

    print(f"\nпул: размер {pool._pool.get_size()}, свободно {pool._pool.get_idle_size()}")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
