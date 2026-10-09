"""Профиль пути «запрос → PostgreSQL → ответ»: куда уходит ~1 мс на запрос.

Четыре варианта одного и того же чтения (100 строк):

* `/ping` — тот же сервер, без обращения к базе (цена «голого» пути фреймворка);
* `/core` — SQLAlchemy Core: `select(users).limit(100)` + `mappings().all()`;
* `/orm`  — ORM: `select(User).limit(100)` + объекты модели;
* `/raw`  — соединение напрямую, без сессии: `text(...)` + `mappings().all()`.

Для каждого варианта печатается:
1. чистое время запроса (без профилировщика) и разложение по блокам внутри хендлера:
   открытие сессии, `execute` (круг до базы), маппинг строк, остаток хендлера;
2. cProfile: self time по категориям (кто жжёт CPU) и топ функций.

Важно: «httpx (клиент замера)» — это накладные расходы самого измерителя в том же
процессе, а не сервер; «ожидание сокета» — время, пока клиент ждёт ответ базы.

Запуск: .venv/bin/python bench/profile_db_path.py [--requests 400]
"""
from __future__ import annotations

import argparse
import asyncio
import cProfile
import os
import pstats
import time
from collections import defaultdict
from typing import TYPE_CHECKING

import httpx
import asyncpg
from sqlalchemy import Column, Integer, String, func, insert, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from ferronit import Ferronit

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

DB_URL = os.environ.get(
    "BENCH_DB_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/postgres"
)

# ── схема и данные ───────────────────────────────────────────────────────────


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String)
    email = Column(String)


engine = create_async_engine(DB_URL)
factory = async_sessionmaker(engine, expire_on_commit=False)
users = User.__table__
_pg_pool: asyncpg.Pool | None = None

# debug=True: исключения пробрасываются наружу, а не превращаются в 500 без следа
app = Ferronit(debug=True)

timings: dict[str, list[float]] = defaultdict(list)


def _mark(name: str, t0: float) -> float:
    """Отмечает блок: пишет микросекунды с момента t0 и возвращает новую точку отсчёта."""
    now = time.perf_counter()
    timings[name].append((now - t0) * 1e6)
    return now


@app.route("/ping")
async def ping(req):
    return {"ok": True}


@app.route("/core")
async def core(req):
    t0 = time.perf_counter()
    async with factory() as session:
        t1 = _mark("открытие сессии", t0)
        result = await session.execute(select(users).limit(100))
        t2 = _mark("execute (круг до базы)", t1)
        rows = [dict(r) for r in result.mappings().all()]
        t3 = _mark("маппинг строк", t2)
    _mark("сессия целиком", t0)
    resp = {"users": rows}
    _mark("остаток хендлера", t3)
    return resp


@app.route("/orm")
async def orm_route(req):
    t0 = time.perf_counter()
    async with factory() as session:
        t1 = _mark("открытие сессии", t0)
        result = await session.execute(select(User).limit(100))
        t2 = _mark("execute (круг до базы)", t1)
        objs = result.scalars().all()
        rows = [{"id": u.id, "name": u.name, "email": u.email} for u in objs]
        t3 = _mark("маппинг строк", t2)
    _mark("сессия целиком", t0)
    resp = {"users": rows}
    _mark("остаток хендлера", t3)
    return resp


@app.route("/asyncpg")
async def asyncpg_route(req):
    """Тот же запрос вообще без SQLAlchemy: сырой драйвер."""
    t0 = time.perf_counter()
    async with _pg_pool.acquire() as conn:
        t1 = _mark("открытие сессии", t0)
        records = await conn.fetch("SELECT id, name, email FROM users LIMIT 100")
        t2 = _mark("execute (круг до базы)", t1)
        rows = [dict(r) for r in records]
        t3 = _mark("маппинг строк", t2)
    _mark("сессия целиком", t0)
    resp = {"users": rows}
    _mark("остаток хендлера", t3)
    return resp


@app.route("/raw")
async def raw(req):
    t0 = time.perf_counter()
    async with engine.connect() as conn:
        t1 = _mark("открытие сессии", t0)
        result = await conn.execute(text("SELECT id, name, email FROM users LIMIT 100"))
        t2 = _mark("execute (круг до базы)", t1)
        rows = [dict(r) for r in result.mappings().all()]
        t3 = _mark("маппинг строк", t2)
    _mark("сессия целиком", t0)
    resp = {"users": rows}
    _mark("остаток хендлера", t3)
    return resp


# ── прогон ───────────────────────────────────────────────────────────────────


async def prepare(rows: int = 1000) -> None:
    global _pg_pool
    _pg_pool = await asyncpg.create_pool(
        DB_URL.replace("postgresql+asyncpg://", "postgresql://"), min_size=1, max_size=10
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as session:
        count = await session.scalar(select(func.count()).select_from(users))
        if not count or count < rows:
            await session.execute(
                insert(users),
                [{"name": f"user_{i}", "email": f"user_{i}@example.com"} for i in range(1, rows + 1)],
            )
            await session.commit()


async def drive(path: str, n: int) -> float:
    """Прогоняет n запросов и возвращает среднее время запроса в микросекундах."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bench"
    ) as client:
        await client.get(path)  # прогрев
        t0 = time.perf_counter()
        for _ in range(n):
            try:
                resp = await client.get(path)
            except Exception:
                import traceback

                print(f"!! {path}: исключение в хендлере")
                traceback.print_exc()
                raise
            if resp.status_code != 200:
                raise RuntimeError(f"{path}: {resp.status_code}")
        return (time.perf_counter() - t0) / n * 1e6


async def profile(path: str, n: int) -> pstats.Stats:
    timings.clear()
    clean = await drive(path, n)  # без профилировщика — честное время и честные блоки
    blocks = {k: sum(v) / n for k, v in timings.items() if v}

    profiler = cProfile.Profile()
    profiler.enable()
    wall = await drive(path, n)
    profiler.disable()

    print(f"\n=== {path} — запросов на прогон: {n} ===")
    print(f"чистое время запроса: {clean:,.0f} мкс (с профилировщиком {wall:,.0f} мкс, накладные ~×{wall / clean:.1f})")
    for block in (
        "сессия целиком",
        "открытие сессии",
        "execute (круг до базы)",
        "маппинг строк",
        "остаток хендлера",
    ):
        if block in blocks:
            print(f"  {block:24s} {blocks[block]:7.1f} мкс  ({blocks[block] / clean * 100:4.1f}% чистого времени)")
    return pstats.Stats(profiler)


def bucket_of(file: str, name: str) -> str:
    """Категория функции — по файлу и имени."""
    if "sqlalchemy" in file:
        return "SQLAlchemy"
    if "asyncpg" in file:
        return "asyncpg"
    if file.endswith("/ferronit/core/app.py") or "/site-packages/ferronit/" in file:
        return "Ferronit (Python-слой)"
    if "httpx" in file or "httpcore" in file:
        return "httpx (клиент замера, НЕ сервер)"
    if file == "~":
        if "poll" in name or "'send'" in name or "'recv'" in name:
            return "ожидание сокета базы (ядро)"
        if "json" in name or "gzip_compress" in name:
            return "Rust-ядро: json/gzip"
        return "builtins: прочее"
    if "asyncio" in file:
        return "asyncio"
    if "greenlet" in file:
        return "greenlet (мост SQLAlchemy)"
    if "profile_db_path" in file:
        return "хендлер (наш код)"
    return "прочее"


def report_buckets(stats: pstats.Stats, n: int, top: int = 10) -> None:
    """Кто жжёт CPU: self time по категориям + топ функций."""
    buckets: dict[str, float] = defaultdict(float)
    for func, (_cc, _nc, tt, _ct, _callers) in stats.stats.items():
        file, _line, name = func
        buckets[bucket_of(file, name)] += tt

    total = sum(buckets.values()) or 1.0
    print("  процессорное время по категориям (self time):")
    for key, tt in sorted(buckets.items(), key=lambda kv: -kv[1]):
        print(f"    {key:36s} {tt / n * 1e6:8.1f} мкс  {tt / total * 100:5.1f}%")

    print(f"  топ-{top} функций по self time:")
    for func, (_cc, _nc, tt, _ct, _callers) in sorted(stats.stats.items(), key=lambda kv: -kv[1][2])[:top]:
        file, line, name = func
        short = "/".join(file.split("/")[-2:])
        print(f"    {tt / n * 1e6:8.1f} мкс  {name[:50]}  ({short}:{line})")


async def run(args: argparse.Namespace) -> None:
    await prepare()
    for path in ("/ping", "/asyncpg", "/raw", "/core", "/orm"):
        # пул соединений привязан к текущему циклу — не тащим его между прогонами
        await engine.dispose()
        n = args.requests * 5 if path == "/ping" else args.requests
        report_buckets(await profile(path, n), n)
    await engine.dispose()
    if _pg_pool is not None:
        await _pg_pool.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--requests", type=int, default=400)
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
