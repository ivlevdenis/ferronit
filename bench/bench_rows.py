"""Ferrox-модель vs Litestar vs FastAPI: ответ с 1 / 100 / 1000 строками.

Два быстрых модельных пути на сыром asyncpg и два классических ORM:
  * ferrox-model      — `ferrox.contrib.rawmodel` (slots-датакласс из строки);
  * litestar-msgspec — `msgspec.Struct` из строки, Litestar сериализует его сам;
  * fastapi-orm      — SQLAlchemy ORM в FastAPI;
  * litestar-orm     — SQLAlchemy ORM в Litestar.
Замер только через `ab` (политика стенда), granian, 1 воркер, лучшее из прогонов.

ferrox-msgspec и litestar-msgspec — одинаковый asyncpg-запрос и msgspec-сериализация,
поэтому их разница — это чистый фреймворк.

Запуск: .venv/bin/python bench/bench_rows.py
Требует PostgreSQL на :5432; таблицу `bench_rows` с 1000 строк скрипт создаёт сам.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV_PY = HERE.parent / ".venv" / "bin" / "python"
DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
ROWS_LIST = (1, 100, 1000)
PORTS = {
    "ferrox-model": 8231,
    "litestar-msgspec": 8232,
    "fastapi-orm": 8233,
    "litestar-orm": 8234,
    "ferrox-msgspec": 8235,
    "django-orm": 8236,
    "ferrox-asyncpg": 8237,
    "litestar-asyncpg": 8238,
}
SERVER = os.environ.get("BENCH_SERVER", "granian")
REQUESTS = 5000
PING_REQUESTS = 10000
RUNS = 2

FERROX_APP = '''
import asyncio
import os

import msgspec
from ferrox import Response, Ferrox
from ferrox.contrib.rawdb import RawUnitOfWork, create_raw_pool
from ferrox.contrib.rawmodel import Model

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
ROWS = int(os.environ.get("BENCH_ROWS", "100"))


class BenchRow(Model):
    __table__ = "bench_rows"
    id: int = 0
    name: str = ""
    email: str = ""


_pool = None
_lock = asyncio.Lock()


async def get_pool():
    """Пул создаём лениво: на импорте цикл событий сервера ещё не запущен."""
    global _pool
    if _pool is None:
        async with _lock:
            if _pool is None:
                _pool = await create_raw_pool(DSN, min_size=1, max_size=16)
    return _pool


app = Ferrox()


@app.route("/rows")
async def rows(req):
    async with RawUnitOfWork(await get_pool(), readonly=True) as uow:
        models = await uow.model(BenchRow).list(limit=ROWS)
    # Модели — msgspec.Struct: сериализация в C, без dict-проекции.
    body = msgspec.json.encode({"rows": models})
    return Response(body, content_type="application/json")


@app.route("/ping")
async def ping(req):
    return {"ok": True}
'''

LITESTAR_MSGSEC_APP = '''
import asyncio
import os

import asyncpg
import msgspec
from litestar import Litestar, get

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
ROWS = int(os.environ.get("BENCH_ROWS", "100"))


class BenchRow(msgspec.Struct):
    id: int
    name: str
    email: str


_pool = None
_lock = asyncio.Lock()


async def get_pool():
    """Пул создаём лениво: на импорте цикл событий сервера ещё не запущен."""
    global _pool
    if _pool is None:
        async with _lock:
            if _pool is None:
                _pool = await asyncpg.create_pool(DSN, min_size=1, max_size=16)
    return _pool


@get("/rows")
async def rows() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        records = await conn.fetch("SELECT id, name, email FROM bench_rows LIMIT $1", ROWS)
    return {"rows": [BenchRow(*record) for record in records]}


@get("/ping")
async def ping() -> dict:
    return {"ok": True}


app = Litestar(route_handlers=[rows, ping])
'''

FERROX_MSGSEC_APP = '''
import asyncio
import os

import asyncpg
import msgspec
from ferrox import Response, Ferrox

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
ROWS = int(os.environ.get("BENCH_ROWS", "100"))


class BenchRow(msgspec.Struct):
    id: int
    name: str
    email: str


_pool = None
_lock = asyncio.Lock()


async def get_pool():
    """Пул создаём лениво: на импорте цикл событий сервера ещё не запущен."""
    global _pool
    if _pool is None:
        async with _lock:
            if _pool is None:
                _pool = await asyncpg.create_pool(DSN, min_size=1, max_size=16)
    return _pool


app = Ferrox()


@app.route("/rows")
async def rows(req):
    pool = await get_pool()
    async with pool.acquire() as conn:
        records = await conn.fetch("SELECT id, name, email FROM bench_rows LIMIT $1", ROWS)
    body = msgspec.json.encode({"rows": [BenchRow(*record) for record in records]})
    return Response(body, content_type="application/json")


@app.route("/ping")
async def ping(req):
    return {"ok": True}
'''

FERROX_ASYNCPG_APP = '''
import asyncio
import os

import asyncpg
from ferrox import Ferrox

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
ROWS = int(os.environ.get("BENCH_ROWS", "100"))

_pool = None
_lock = asyncio.Lock()


async def get_pool():
    """Пул создаём лениво: на импорте цикл событий сервера ещё не запущен."""
    global _pool
    if _pool is None:
        async with _lock:
            if _pool is None:
                _pool = await asyncpg.create_pool(DSN, min_size=1, max_size=16)
    return _pool


app = Ferrox()


@app.route("/rows")
async def rows(req):
    pool = await get_pool()
    async with pool.acquire() as conn:
        records = await conn.fetch("SELECT id, name, email FROM bench_rows LIMIT $1", ROWS)
    return {"rows": [dict(record) for record in records]}


@app.route("/ping")
async def ping(req):
    return {"ok": True}
'''

LITESTAR_ASYNCPG_APP = '''
import asyncio
import os

import asyncpg
from litestar import Litestar, get

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
ROWS = int(os.environ.get("BENCH_ROWS", "100"))

_pool = None
_lock = asyncio.Lock()


async def get_pool():
    """Пул создаём лениво: на импорте цикл событий сервера ещё не запущен."""
    global _pool
    if _pool is None:
        async with _lock:
            if _pool is None:
                _pool = await asyncpg.create_pool(DSN, min_size=1, max_size=16)
    return _pool


@get("/rows")
async def rows() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        records = await conn.fetch("SELECT id, name, email FROM bench_rows LIMIT $1", ROWS)
    return {"rows": [dict(record) for record in records]}


@get("/ping")
async def ping() -> dict:
    return {"ok": True}


app = Litestar(route_handlers=[rows, ping])
'''

FASTAPI_ORM_APP = '''
import os

from fastapi import FastAPI
from sqlalchemy import Column, Integer, String, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

DSN = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/postgres"
ROWS = int(os.environ.get("BENCH_ROWS", "100"))


class Base(DeclarativeBase):
    pass


class BenchRow(Base):
    __tablename__ = "bench_rows"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False)


engine = create_async_engine(DSN, echo=False)
factory = async_sessionmaker(engine, expire_on_commit=False)

app = FastAPI()


@app.get("/rows")
async def rows():
    async with factory() as session:
        result = await session.execute(select(BenchRow).limit(ROWS))
        models = result.scalars().all()
    return {"rows": [{"id": r.id, "name": r.name, "email": r.email} for r in models]}


@app.get("/ping")
async def ping():
    return {"ok": True}
'''

LITESTAR_ORM_APP = '''
import os

from litestar import Litestar, get
from sqlalchemy import Column, Integer, String, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

DSN = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/postgres"
ROWS = int(os.environ.get("BENCH_ROWS", "100"))


class Base(DeclarativeBase):
    pass


class BenchRow(Base):
    __tablename__ = "bench_rows"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False)


engine = create_async_engine(DSN, echo=False)
factory = async_sessionmaker(engine, expire_on_commit=False)


@get("/rows")
async def rows() -> dict:
    async with factory() as session:
        result = await session.execute(select(BenchRow).limit(ROWS))
        models = result.scalars().all()
    return {"rows": [{"id": r.id, "name": r.name, "email": r.email} for r in models]}


@get("/ping")
async def ping() -> dict:
    return {"ok": True}


app = Litestar(route_handlers=[rows, ping])
'''

APPS = {
    "ferrox-model": FERROX_APP,
    "litestar-msgspec": LITESTAR_MSGSEC_APP,
    "fastapi-orm": FASTAPI_ORM_APP,
    "litestar-orm": LITESTAR_ORM_APP,
    "ferrox-msgspec": FERROX_MSGSEC_APP,
    "ferrox-asyncpg": FERROX_ASYNCPG_APP,
    "litestar-asyncpg": LITESTAR_ASYNCPG_APP,
}


def seed() -> None:
    """Готовит таблицу ``bench_rows`` ровно с 1000 строками."""
    import asyncpg

    async def _seed() -> None:
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS bench_rows ("
                "id serial PRIMARY KEY, name text NOT NULL, email text NOT NULL)"
            )
            await conn.execute("TRUNCATE bench_rows RESTART IDENTITY")
            await conn.execute(
                "INSERT INTO bench_rows (name, email) "
                "SELECT 'user_' || g, 'u' || g || '@example.com' "
                "FROM generate_series(1, 1000) AS g"
            )
        finally:
            await conn.close()

    asyncio.run(_seed())


def start(name: str, port: int, rows: int) -> subprocess.Popen:
    """Пишет приложение и поднимает его одним воркером на выбранном сервере."""
    if name == "django-orm":
        # Django — отдельный пакет bench/django_bench/ (settings/models/urls/asgi).
        cmd = [
            str(VENV_PY), "-m", SERVER, "--interface", "asgi",
            "--host", "127.0.0.1", "--port", str(port), "--workers", "1",
            "--no-ws", "--log-level", "warning", "django_bench.asgi:application",
        ]
    else:
        module = name.replace("-", "_")
        (HERE / f"_bench_rows_{module}.py").write_text(APPS[name])
        cmd = [
            str(VENV_PY), "-m", SERVER, "--interface", "asgi",
            "--host", "127.0.0.1", "--port", str(port), "--workers", "1",
            "--no-ws", "--log-level", "warning", f"_bench_rows_{module}:app",
        ]
    return subprocess.Popen(
        cmd,
        cwd=HERE,
        env={**os.environ, "BENCH_ROWS": str(rows), "DJANGO_SETTINGS_MODULE": "django_bench.settings"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_ready(port: int, timeout: float = 20.0) -> None:
    """Ждёт готовности сервера, дёргая ``/ping``."""
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise TimeoutError(f"сервер на :{port} не поднялся")


def ab_reqs(port: int, path: str, n: int) -> float:
    """req/s по ApacheBench."""
    out = subprocess.run(
        ["ab", "-n", str(n), "-c", "50", "-k", f"http://127.0.0.1:{port}{path}"],
        capture_output=True, text=True, timeout=300,
    ).stdout
    match = re.search(r"Requests per second:\s+([\d.]+)", out)
    if not match:
        raise RuntimeError(f"ab не дал результат для {path}:\n{out[-400:]}")
    return float(match.group(1))


def run_case(rows: int) -> dict[str, dict[str, float]]:
    """Один замер на фиксированном числе строк; возвращает лучшее из ``RUNS``."""
    procs = {name: start(name, port, rows) for name, port in PORTS.items()}
    try:
        for port in PORTS.values():
            wait_ready(port)
        best: dict[str, dict[str, float]] = {name: {} for name in PORTS}
        for _ in range(RUNS):
            for name, port in PORTS.items():
                rows_rps = ab_reqs(port, "/rows", REQUESTS)
                ping_rps = ab_reqs(port, "/ping", PING_REQUESTS)
                best[name]["rows"] = max(best[name].get("rows", 0.0), rows_rps)
                best[name]["ping"] = max(best[name].get("ping", 0.0), ping_rps)
        return best
    finally:
        for proc in procs.values():
            proc.terminate()
        for proc in procs.values():
            proc.wait()


def main() -> None:
    seed()
    names = list(PORTS)
    print(f"сервер {SERVER}, 1 воркер, ab -c 50 -k, лучшее из {RUNS}\n")

    header = f"{'строк':>6} | " + " | ".join(f"{name:>17}" for name in names)
    print(header)
    print("-" * len(header))
    pings: dict[int, dict[str, float]] = {}
    for rows in ROWS_LIST:
        result = run_case(rows)
        pings[rows] = {name: result[name]["ping"] for name in names}
        cells = " | ".join(f"{result[name]['rows']:>17,.0f}" for name in names)
        print(f"{rows:>6} | {cells}")

    print("\n/ping, req/s (тот же путь, без БД):")
    for name in names:
        print(f"  {name:22s} {pings[ROWS_LIST[0]][name]:>12,.0f}")


if __name__ == "__main__":
    main()
