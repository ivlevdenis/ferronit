"""Прямой замер PostgreSQL без фреймворка: asyncpg → SQLAlchemy Core → ORM.

Смысл: один и тот же запрос (`SELECT ... LIMIT 100`) прогоняется тремя способами
доступа к базе, чтобы видеть, сколько даёт сам драйвер, сколько съедает SQLAlchemy
Core и сколько — ORM-маппинг. Плюс контрольный `SELECT 1` для задержки сети.

Запуск (нужен живой контейнер ferronit-pg и база bench):
    .venv/bin/python bench/pg_direct.py                      # asyncpg, 50 клиентов
    .venv/bin/python bench/pg_direct.py --mode core
    .venv/bin/python bench/pg_direct.py --mode orm
    .venv/bin/python bench/pg_direct.py --clients 10 --requests 2000
    .venv/bin/python bench/pg_direct.py --skip-prepare       # когда данные уже готовы

Замер счётный: несколько процессов с `--skip-prepare` можно запускать параллельно,
чтобы понять, упирается ли результат в Python-клиент.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import time
from collections.abc import Awaitable, Callable

import asyncpg
from sqlalchemy import Column, Integer, String, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

DB_URL = os.environ.get("BENCH_DB_URL", "postgresql://postgres:postgres@127.0.0.1:5432/bench")
SA_URL = DB_URL.replace("postgresql://", "postgresql+asyncpg://")

SELECT_100 = "SELECT id, name, email, active, score FROM users LIMIT 100"

DDL = """
DROP TABLE IF EXISTS users;
CREATE TABLE users (
    id      serial PRIMARY KEY,
    name    text NOT NULL,
    email   text NOT NULL,
    active  boolean NOT NULL DEFAULT true,
    score   double precision NOT NULL DEFAULT 0
);
"""


# Запись: строка за запрос, с RETURNING — так же, как делает приложение.
INSERT_SQL = "INSERT INTO users (name, email) VALUES ($1, $2) RETURNING id, name"


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String)
    email = Column(String)


users_table = User.__table__


# ── наполнение ───────────────────────────────────────────────────────────────


async def prepare(rows: int) -> None:
    conn = await asyncpg.connect(DB_URL)
    try:
        await conn.execute(DDL)
        await conn.execute(
            "INSERT INTO users (name, email, active, score) "
            "SELECT 'user_' || g, 'user_' || g || '@example.com', g % 2 = 0, g * 1.5 "
            "FROM generate_series(1, $1) AS g",
            rows,
        )
    finally:
        await conn.close()


# ── три способа прочитать те же 100 строк ────────────────────────────────────


async def worker_asyncpg(pool: asyncpg.Pool, requests: int, out: list[float], op: str) -> None:
    async with pool.acquire() as conn:
        for i in range(requests):
            t0 = time.perf_counter()
            if op == "insert":
                await conn.fetchrow(INSERT_SQL, f"bench_{id(out) % 99999}_{i}", "bench@example.com")
            else:
                await conn.fetch(SELECT_100)
            out.append(time.perf_counter() - t0)


async def worker_psycopg(pool, requests: int, out: list[float], op: str) -> None:
    """Тот же запрос, но через psycopg 3 (async)."""
    async with pool.connection() as conn:
        for i in range(requests):
            t0 = time.perf_counter()
            if op == "insert":
                cur = await conn.execute(
                    "INSERT INTO users (name, email) VALUES (%s, %s) RETURNING id, name",
                    (f"bench_{id(out) % 99999}_{i}", "bench@example.com"),
                )
            else:
                cur = await conn.execute(SELECT_100)
            await cur.fetchall()
            out.append(time.perf_counter() - t0)


async def worker_core(engine, requests: int, out: list[float], op: str) -> None:
    from sqlalchemy import insert as sa_insert

    async with engine.connect() as conn:
        for i in range(requests):
            t0 = time.perf_counter()
            if op == "insert":
                result = await conn.execute(
                    sa_insert(users_table)
                    .values(name=f"bench_{id(out) % 99999}_{i}", email="bench@example.com")
                    .returning(users_table.c.id, users_table.c.name)
                )
                result.first()
                await conn.commit()
            else:
                result = await conn.execute(select(users_table).limit(100))
                [dict(r) for r in result.mappings().all()]
            out.append(time.perf_counter() - t0)


async def worker_orm(factory, requests: int, out: list[float], op: str) -> None:
    async with factory() as session:
        for i in range(requests):
            t0 = time.perf_counter()
            if op == "insert":
                session.add(User(name=f"bench_{id(out) % 99999}_{i}", email="bench@example.com"))
                await session.commit()
            else:
                result = await session.execute(select(User).limit(100))
                [{"id": u.id, "name": u.name, "email": u.email} for u in result.scalars().all()]
            out.append(time.perf_counter() - t0)


async def measure(mode: str, clients: int, requests: int, op: str = "select") -> None:
    engine = None
    factory = None
    pool = None
    psy_pool = None
    worker: Callable[..., Awaitable[None]]

    if mode == "asyncpg":
        pool = await asyncpg.create_pool(DB_URL, min_size=clients, max_size=clients)
        worker = lambda n, out: worker_asyncpg(pool, n, out, op)  # noqa: E731
    elif mode == "psycopg":
        from psycopg_pool import AsyncConnectionPool

        # autocommit: каждый SELECT — своя транзакция, как у asyncpg (честное сравнение)
        psy_pool = AsyncConnectionPool(
            DB_URL, min_size=clients, max_size=clients, open=False, kwargs={"autocommit": True}
        )
        await psy_pool.open()
        worker = lambda n, out: worker_psycopg(psy_pool, n, out, op)  # noqa: E731
    else:
        url = SA_URL
        if mode.endswith("-psycopg"):
            url = SA_URL.replace("postgresql+asyncpg://", "postgresql+psycopg://")
        engine = create_async_engine(url, pool_size=clients, max_overflow=0)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        worker = (
            (lambda n, out: worker_core(engine, n, out, op))
            if mode.startswith("core")
            else (lambda n, out: worker_orm(factory, n, out, op))
        )

    try:
        # baseline: стоимость круга до базы на простом запросе
        ping: list[float] = []
        if pool is not None:
            async with pool.acquire() as conn:
                for _ in range(50):
                    t0 = time.perf_counter()
                    await conn.fetchval("SELECT 1")
                    ping.append((time.perf_counter() - t0) * 1e6)
            version = await pool.fetchval("SHOW server_version")
        elif psy_pool is not None:
            async with psy_pool.connection() as conn:
                for _ in range(50):
                    t0 = time.perf_counter()
                    await conn.execute("SELECT 1")
                    ping.append((time.perf_counter() - t0) * 1e6)
                version = (await (await conn.execute("SHOW server_version")).fetchone())[0]
        else:
            async with engine.connect() as conn:
                for _ in range(50):
                    t0 = time.perf_counter()
                    await conn.execute(text("SELECT 1"))
                    ping.append((time.perf_counter() - t0) * 1e6)
                version = (await conn.execute(text("SHOW server_version"))).scalar()

        durations: list[float] = []
        t0 = time.perf_counter()
        await asyncio.gather(*(worker(requests, durations) for _ in range(clients)))
        wall = time.perf_counter() - t0

        durations.sort()
        pct = lambda p: durations[min(len(durations) - 1, int(len(durations) * p))] * 1e3  # noqa: E731
        print(
            f"{mode:8s} PG {version.split()[0]:6s} {op:6s} клиентов {clients:3d}: "
            f"{len(durations) / wall:9,.0f} запросов/с   "
            f"latency p50 {pct(0.50):5.2f} / p95 {pct(0.95):5.2f} мс   "
            f"SELECT 1 p50 {statistics.median(ping):6.1f} мкс"
        )
    finally:
        if pool is not None:
            await pool.close()
        if psy_pool is not None:
            await psy_pool.close()
        if engine is not None:
            await engine.dispose()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--mode",
        default="asyncpg",
        choices=("asyncpg", "psycopg", "core", "orm", "core-psycopg", "orm-psycopg"),
    )
    p.add_argument("--op", default="select", choices=("select", "insert"), help="что мерим")
    p.add_argument("--clients", type=int, default=50, help="одновременных соединений")
    p.add_argument("--requests", type=int, default=1000, help="запросов на клиента")
    p.add_argument("--rows", type=int, default=1000, help="строк в таблице users")
    p.add_argument("--skip-prepare", action="store_true", help="не пересоздавать данные")
    a = p.parse_args()
    if not a.skip_prepare:
        asyncio.run(prepare(a.rows))
    asyncio.run(measure(a.mode, a.clients, a.requests, a.op))


if __name__ == "__main__":
    main()
