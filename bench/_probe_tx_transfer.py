"""Пример: много клиентов переводят деньги — как разводятся транзакции.

Один пул соединений, 50 параллельных клиентов, каждый перевод — своя транзакция
(`pool.uow()`, то есть `BEGIN` … `COMMIT` на одном соединении из пула). Проверяется
главный инвариант банка: **сумма балансов не меняется** — деньги не создаются и не
исчезают, сколько бы клиентов ни работало одновременно.

Два способа сделать перевод:
    naive    — прочитать балансы в Python, посчитать, записать обратно;
    correct  — списание делает база одним атомарным `UPDATE ... WHERE balance >= $1`,
               зачисление — в той же транзакции; не прошло условие — откат.

Запуск: .venv/bin/python bench/_probe_tx_transfer.py
"""

from __future__ import annotations

import asyncio
import time

import asyncpg

from ferronit.contrib.rawmodel import Model

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
ACCOUNTS = 200
START_BALANCE = 1000
CLIENTS = 50
TRANSFERS_PER_CLIENT = 40
AMOUNT = 1
DDL = """
CREATE TABLE IF NOT EXISTS tx_accounts (
    id int PRIMARY KEY, balance int NOT NULL CHECK (balance >= 0));
"""


class AccountRow(Model):
    """Строка таблицы ``tx_accounts`` (модель слоя данных Ferronit)."""

    __table__ = "tx_accounts"
    id: int = 0
    balance: int = 0


class NotEnoughFunds(Exception):
    """Недостаточно средств — транзакция должна откатиться целиком."""


async def reset(pool) -> None:
    """Счета с одинаковым начальным балансом."""
    async with pool.uow() as uow:
        conn = uow.connection
        await conn.execute(DDL)
        await conn.execute("TRUNCATE tx_accounts")
        await conn.executemany(
            "INSERT INTO tx_accounts (id, balance) VALUES ($1, $2)",
            [(i, START_BALANCE) for i in range(ACCOUNTS)],
        )


async def transfer_naive(pool, src: int, dst: int) -> None:
    """Прочитал — посчитал в Python — записал: под конкуренцией ломает инвариант."""
    async with pool.uow() as uow:
        accounts = uow.model(AccountRow)
        a = await accounts.get(src)
        b = await accounts.get(dst)
        await asyncio.sleep(0.0002)          # окно между чтением и записью
        if a.balance < AMOUNT:
            raise NotEnoughFunds
        await accounts.update(src, {"balance": a.balance - AMOUNT})
        await accounts.update(dst, {"balance": b.balance + AMOUNT})


async def transfer_atomic(pool, src: int, dst: int) -> None:
    """Списание и зачисление — одной транзакцией, арифметику делает база."""
    async with pool.uow() as uow:
        conn = uow.connection
        debited = await conn.fetchval(
            "UPDATE tx_accounts SET balance = balance - $1 "
            "WHERE id = $2 AND balance >= $1 RETURNING balance",
            AMOUNT,
            src,
        )
        if debited is None:
            raise NotEnoughFunds              # откат: деньги не ушли никуда
        await conn.execute(
            "UPDATE tx_accounts SET balance = balance + $1 WHERE id = $2", AMOUNT, dst
        )


async def worker(pool, client: int, transfer) -> int:
    """Клиент делает серию переводов между случайными счетами."""
    done = 0
    for step in range(TRANSFERS_PER_CLIENT):
        src = (client * 7 + step) % ACCOUNTS
        dst = (client * 13 + step * 3 + 1) % ACCOUNTS
        if src == dst:
            continue
        try:
            await transfer(pool, src, dst)
            done += 1
        except NotEnoughFunds:
            pass
    return done


async def total(pool) -> int:
    """Сумма всех балансов — инвариант, который не должен меняться."""
    async with pool.uow(readonly=True) as uow:
        return await uow.connection.fetchval("SELECT sum(balance) FROM tx_accounts")


async def main() -> None:
    """Прогнать оба способа на одинаковой нагрузке и сверить инвариант."""
    from ferronit.contrib.rawdb import create_pool

    pool = await create_pool(DSN, min_size=CLIENTS, max_size=CLIENTS)
    expected = ACCOUNTS * START_BALANCE

    for label, transfer in (
        ("naive (чтение-правка-запись)", transfer_naive),
        ("атомарный UPDATE в одной UoW", transfer_atomic),
    ):
        await reset(pool)
        before = await total(pool)
        start = time.perf_counter()
        done = await asyncio.gather(*[worker(pool, c, transfer) for c in range(CLIENTS)])
        elapsed = time.perf_counter() - start
        after = await total(pool)
        transfers = sum(done)
        print(
            f"{label:<32} переводов {transfers:>5}, {transfers / elapsed:>7,.0f} переводов/с, "
            f"сумма {before} -> {after} (ожидалось {expected}), "
            f"расхождение {after - before:+d}"
        )

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
