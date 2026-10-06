"""Прямой бенч PostgreSQL: самодельный клиент по проводу против asyncpg.

Замеряет одно и то же (``SELECT id, name, email FROM users LIMIT 100``) двумя клиентами
в одном процессе и на одинаковых условиях: N клиентов, M запросов на клиента.

Запуск:
    .venv/bin/python bench/wire_bench.py --driver wire --clients 10 --requests 1000
    .venv/bin/python bench/wire_bench.py --driver wire --clients 10 --requests 1000 --pipeline 10
    .venv/bin/python bench/wire_bench.py --driver asyncpg --clients 10 --requests 1000

Пайплайн (``--pipeline K``) отправляет K запросов одним flush — это то, что делает драйвер
с батчингом; в asyncpg-режиме ключ не используется.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import asyncpg  # noqa: E402

from pg_wire import WireConnection  # noqa: E402

DSN_PARTS = {"host": "127.0.0.1", "port": 5432, "user": "postgres", "password": "postgres"}
SELECT_100 = "SELECT id, name, email FROM users LIMIT 100"
INSERT_ONE = "INSERT INTO users (name, email) VALUES ('{name}', 'wire@example.com') RETURNING id"


async def worker_wire(
    op: str, requests: int, pipeline: int, prepared: bool, sql: str, out: list[float]
) -> None:
    """Один клиент самодельного протокола: своё соединение, свои запросы."""
    conn = await WireConnection.connect(**DSN_PARTS)
    try:
        if prepared:
            # prepared + бинарный формат: сервер парсит запрос один раз
            statement = await conn.prepare(sql)
            for _ in range(requests):
                started = time.perf_counter()
                await conn.fetch_prepared(statement)
                out.append((time.perf_counter() - started) / 1e-6)
            return
        if pipeline > 1:
            batches, remainder = divmod(requests, pipeline)
            for index in range(batches):
                started = time.perf_counter()
                await conn.query_pipelined(SELECT_100, pipeline)
                elapsed = (time.perf_counter() - started) / pipeline / 1e-6
                out.extend([elapsed] * pipeline)
            for _ in range(remainder):
                started = time.perf_counter()
                await conn.query(SELECT_100)
                out.append((time.perf_counter() - started) / 1e-6)
            return
        for index in range(requests):
            started = time.perf_counter()
            if op == "insert":
                await conn.query(INSERT_ONE.format(name=f"wire_{id(out) % 9999}_{index}"))
            else:
                await conn.query(sql)
            out.append((time.perf_counter() - started) / 1e-6)
    finally:
        conn.close()


async def worker_asyncpg(
    pool: asyncpg.Pool, op: str, requests: int, sql: str, out: list[float]
) -> None:
    """Тот же замер штатным драйвером — как эталон."""
    async with pool.acquire() as conn:
        for index in range(requests):
            started = time.perf_counter()
            if op == "insert":
                await conn.fetchrow(
                    "INSERT INTO users (name, email) VALUES ($1, $2) RETURNING id",
                    f"asyncpg_{id(out) % 9999}_{index}",
                    "asyncpg@example.com",
                )
            else:
                await conn.fetch(sql)
            out.append((time.perf_counter() - started) / 1e-6)


async def measure(
    driver: str, op: str, clients: int, requests: int, pipeline: int, prepared: bool, limit: int
) -> None:
    """Run the benchmark and print one line in the same shape as `pg_direct.py`."""
    sql = f"SELECT id, name, email FROM users LIMIT {limit}"
    version = "?"
    pool = None
    if driver == "asyncpg":
        pool = await asyncpg.create_pool(
            "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
            min_size=clients,
            max_size=clients,
        )
        version = (await pool.fetchval("SHOW server_version")).split()[0]
    else:
        probe = await WireConnection.connect(**DSN_PARTS)
        try:
            rows = await probe.query("SHOW server_version")
            version = rows[0]["server_version"].split()[0]
        finally:
            probe.close()

    durations: list[float] = []
    started = time.perf_counter()
    try:
        if driver == "asyncpg":
            await asyncio.gather(
                *(worker_asyncpg(pool, op, requests, sql, durations) for _ in range(clients))
            )
        else:
            await asyncio.gather(
                *(
                    worker_wire(op, requests, pipeline, prepared, sql, durations)
                    for _ in range(clients)
                )
            )
    finally:
        if pool is not None:
            await pool.close()
    wall = time.perf_counter() - started

    durations.sort()
    quantile = lambda p: durations[min(len(durations) - 1, int(len(durations) * p))]  # noqa: E731
    note = f" пайплайн {pipeline}" if driver == "wire" and pipeline > 1 else ""
    if driver == "wire" and prepared and op == "select":
        note = " prepared + бинарный формат"
    note += f" строк {limit}" if op == "select" else ""
    print(
        f"{driver:8s} PG {version:6s} {op:6s} клиентов {clients:3d}: "
        f"{len(durations) / wall:9,.0f} запросов/с   "
        f"latency p50 {quantile(0.50):6.1f} / p95 {quantile(0.95):6.1f} мкс{note}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--driver", default="wire", choices=("wire", "asyncpg"))
    parser.add_argument("--op", default="select", choices=("select", "insert"))
    parser.add_argument("--clients", type=int, default=10)
    parser.add_argument("--requests", type=int, default=1000, help="запросов на клиента")
    parser.add_argument("--pipeline", type=int, default=1, help="запросов в одном flush (wire)")
    parser.add_argument("--prepared", action="store_true", help="prepared + бинарный формат (wire)")
    parser.add_argument("--limit", type=int, default=100, help="строк в SELECT (select)")
    args = parser.parse_args()
    asyncio.run(
        measure(
            args.driver, args.op, args.clients, args.requests, args.pipeline, args.prepared,
            args.limit,
        )
    )


if __name__ == "__main__":
    main()
