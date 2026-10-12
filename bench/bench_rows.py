"""Ferronit-rawmodel vs Litestar/FastAPI/Django/BlackSheep: ответ с 1 / 100 / 1000 строками.

Ferronit представлен единственным слоем данных — rawmodel (asyncpg + msgspec);
конкуренты — своими ORM/msgspec-путями. Замер только через `ab` (политика стенда),
granian, 1 воркер, лучшее из прогонов.

Запуск: .venv/bin/python bench/bench_rows.py
Требует PostgreSQL на :5432; таблицу `bench_rows` с 1000 строк скрипт создаёт сам.
"""

from __future__ import annotations

import asyncio
import json
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
    "ferronit-model": 8231,
    "litestar-msgspec": 8232,
    "fastapi-orm": 8233,
    "litestar-orm": 8234,
    "django-orm": 8236,
    "litestar-asyncpg": 8238,
    "blacksheep": 8243,
    "litestar-core": 8244,
    "blacksheep-core": 8245,
}
SERVER = os.environ.get("BENCH_SERVER", "granian")
REQUESTS = 5000
PING_REQUESTS = 10000
RUNS = 2
WRITE_REQUESTS = {1: 3000, 100: 800, 1000: 150}
WRITE_APPS = ("ferronit-model", "blacksheep", "litestar-msgspec", "litestar-core", "blacksheep-core")


def bulk_body(op: str, n: int) -> str:
    """JSON-тело батч-записи на ``n`` строк."""
    if op == "insert":
        rows = [{"name": f"n{i}", "email": f"e{i}@x.io"} for i in range(n)]
    else:
        rows = [{"id": i + 1, "name": f"n{i}", "email": f"e{i}@x.io"} for i in range(n)]
    return json.dumps({"rows": rows})

FERRONIT_APP = '''
import os
from typing import Annotated

import msgspec
from ferronit import Response, Ferronit
from ferronit.contrib.rawdb import Pool, create_pool
from ferronit.contrib.rawmodel import Model
from ferronit.di import Container

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
ROWS = int(os.environ.get("BENCH_ROWS", "100"))
ShopPool = Annotated[Pool, "shop"]

UPSERT_SQL = (
    "INSERT INTO bench_rows (id, name, email) VALUES ($1, $2, $3) "
    "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, email = EXCLUDED.email "
    "RETURNING id, name, email"
)


class BenchRow(Model):
    __table__ = "bench_rows"
    id: int = 0
    name: str = ""
    email: str = ""


class BenchRepo:
    def __init__(self, pool: ShopPool):
        self._pool = pool

    async def list_rows(self):
        async with self._pool.uow(readonly=True) as uow:
            return await uow.model(BenchRow).list(limit=ROWS)

    async def bulk_insert(self, rows):
        placeholders = ", ".join(f"(${i * 2 + 1}, ${i * 2 + 2})" for i in range(len(rows)))
        params = [v for r in rows for v in (r["name"], r["email"])]
        sql = f"INSERT INTO bench_rows (name, email) VALUES {placeholders}"
        async with self._pool.uow() as uow:
            await uow.connection.execute(sql, *params)
        return len(rows)

    async def bulk_update(self, rows):
        async with self._pool.uow() as uow:
            await uow.connection.executemany(
                "UPDATE bench_rows SET name = $2, email = $3 WHERE id = $1",
                [(r["id"], r["name"], r["email"]) for r in rows],
            )
        return len(rows)

    async def bulk_upsert(self, rows):
        placeholders = ", ".join(f"(${i * 3 + 1}, ${i * 3 + 2}, ${i * 3 + 3})" for i in range(len(rows)))
        params = [v for r in rows for v in (r["id"], r["name"], r["email"])]
        sql = (
            f"INSERT INTO bench_rows (id, name, email) VALUES {placeholders} "
            "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, email = EXCLUDED.email"
        )
        async with self._pool.uow() as uow:
            await uow.connection.execute(sql, *params)
        return len(rows)


container = Container()
app = Ferronit(container=container)
container.factory(BenchRepo)


@app.lifespan
async def lifespan():
    pool = await create_pool(DSN, min_size=1, max_size=16)
    container.singleton(ShopPool, pool)
    yield
    await pool.close()


def _encode(row):
    return Response(
        msgspec.json.encode({"id": row.id, "name": row.name, "email": row.email}),
        content_type="application/json",
    )


@app.route("/rows")
async def rows(repo: BenchRepo):
    models = await repo.list_rows()
    body = msgspec.json.encode({"rows": models})
    return Response(body, content_type="application/json")


@app.route("/rows", methods=["POST"])
async def insert_rows(req, repo: BenchRepo):
    body = await req.json()
    return {"count": await repo.bulk_insert(body["rows"])}


@app.route("/rows", methods=["PUT"])
async def update_rows(req, repo: BenchRepo):
    body = await req.json()
    return {"count": await repo.bulk_update(body["rows"])}


@app.route("/rows/upsert", methods=["POST"])
async def upsert_rows(req, repo: BenchRepo):
    body = await req.json()
    return {"count": await repo.bulk_upsert(body["rows"])}


@app.route("/ping")
async def ping():
    return {"ok": True}
'''

LITESTAR_MSGSEC_APP = '''
import asyncio
import os

import asyncpg
import msgspec
from litestar import Litestar, Request, get, post, put

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
ROWS = int(os.environ.get("BENCH_ROWS", "100"))
UPSERT_SQL = (
    "INSERT INTO bench_rows (id, name, email) VALUES ($1, $2, $3) "
    "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, email = EXCLUDED.email "
    "RETURNING id, name, email"
)


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


@post("/rows")
async def insert_rows(request: Request) -> dict:
    data = await request.json()
    rows = data["rows"]
    placeholders = ", ".join(f"(${i * 2 + 1}, ${i * 2 + 2})" for i in range(len(rows)))
    params = [v for r in rows for v in (r["name"], r["email"])]
    sql = f"INSERT INTO bench_rows (name, email) VALUES {placeholders}"
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(sql, *params)
    return {"count": len(rows)}


@put("/rows")
async def update_rows(request: Request) -> dict:
    data = await request.json()
    rows = data["rows"]
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            "UPDATE bench_rows SET name = $2, email = $3 WHERE id = $1",
            [(r["id"], r["name"], r["email"]) for r in rows],
        )
    return {"count": len(rows)}


@post("/rows/upsert")
async def upsert_rows(request: Request) -> dict:
    data = await request.json()
    rows = data["rows"]
    placeholders = ", ".join(f"(${i * 3 + 1}, ${i * 3 + 2}, ${i * 3 + 3})" for i in range(len(rows)))
    params = [v for r in rows for v in (r["id"], r["name"], r["email"])]
    sql = (
        f"INSERT INTO bench_rows (id, name, email) VALUES {placeholders} "
        "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, email = EXCLUDED.email"
    )
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(sql, *params)
    return {"count": len(rows)}


@get("/ping")
async def ping() -> dict:
    return {"ok": True}


app = Litestar(route_handlers=[rows, insert_rows, update_rows, upsert_rows, ping])
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

BLACKSHEEP_APP = '''
import asyncio
import os

import asyncpg
import msgspec
from blacksheep import Application, Request, Response
from blacksheep.contents import Content

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
ROWS = int(os.environ.get("BENCH_ROWS", "100"))
UPSERT_SQL = (
    "INSERT INTO bench_rows (id, name, email) VALUES ($1, $2, $3) "
    "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, email = EXCLUDED.email "
    "RETURNING id, name, email"
)


class BenchRow(msgspec.Struct):
    id: int
    name: str
    email: str


_pool = None
_lock = asyncio.Lock()


async def get_pool():
    global _pool
    if _pool is None:
        async with _lock:
            if _pool is None:
                _pool = await asyncpg.create_pool(DSN, min_size=1, max_size=16)
    return _pool


def _json(data):
    return Response(200, content=Content(b"application/json", msgspec.json.encode(data)))


app = Application()


@app.router.get("/rows")
async def rows():
    pool = await get_pool()
    async with pool.acquire() as conn:
        records = await conn.fetch("SELECT id, name, email FROM bench_rows LIMIT $1", ROWS)
    body = msgspec.json.encode({"rows": [BenchRow(*record) for record in records]})
    return Response(200, content=Content(b"application/json", body))


@app.router.post("/rows")
async def insert_rows(request: Request):
    data = await request.json()
    rows = data["rows"]
    placeholders = ", ".join(f"(${i * 2 + 1}, ${i * 2 + 2})" for i in range(len(rows)))
    params = [v for r in rows for v in (r["name"], r["email"])]
    sql = f"INSERT INTO bench_rows (name, email) VALUES {placeholders}"
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(sql, *params)
    return {"count": len(rows)}


@app.router.put("/rows")
async def update_rows(request: Request):
    data = await request.json()
    rows = data["rows"]
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            "UPDATE bench_rows SET name = $2, email = $3 WHERE id = $1",
            [(r["id"], r["name"], r["email"]) for r in rows],
        )
    return {"count": len(rows)}


@app.router.post("/rows/upsert")
async def upsert_rows(request: Request):
    data = await request.json()
    rows = data["rows"]
    placeholders = ", ".join(f"(${i * 3 + 1}, ${i * 3 + 2}, ${i * 3 + 3})" for i in range(len(rows)))
    params = [v for r in rows for v in (r["id"], r["name"], r["email"])]
    sql = (
        f"INSERT INTO bench_rows (id, name, email) VALUES {placeholders} "
        "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, email = EXCLUDED.email"
    )
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(sql, *params)
    return {"count": len(rows)}


@app.router.get("/ping")
async def ping():
    return {"ok": True}
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

LITESTAR_CORE_APP = '''
import os

from litestar import Litestar, Request, get, post, put
from sqlalchemy import Column, Integer, MetaData, String, Table, bindparam, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

DSN = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/postgres"
ROWS = int(os.environ.get("BENCH_ROWS", "100"))

metadata = MetaData()
bench_rows = Table(
    "bench_rows", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String),
    Column("email", String),
)

engine = create_async_engine(DSN)


@get("/rows")
async def rows() -> dict:
    async with engine.connect() as conn:
        result = await conn.execute(select(bench_rows).limit(ROWS))
        records = result.mappings().all()
    return {"rows": [dict(r) for r in records]}


@post("/rows")
async def insert_rows(request: Request) -> dict:
    data = await request.json()
    rows = data["rows"]
    async with engine.begin() as conn:
        await conn.execute(bench_rows.insert(), rows)
    return {"count": len(rows)}


@put("/rows")
async def update_rows(request: Request) -> dict:
    data = await request.json()
    rows = data["rows"]
    async with engine.begin() as conn:
        await conn.execute(
            bench_rows.update().where(bench_rows.c.id == bindparam("b_id")),
            [{"b_id": r["id"], "name": r["name"], "email": r["email"]} for r in rows],
        )
    return {"count": len(rows)}


@post("/rows/upsert")
async def upsert_rows(request: Request) -> dict:
    data = await request.json()
    rows = data["rows"]
    stmt = pg_insert(bench_rows).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[bench_rows.c.id],
        set_={"name": stmt.excluded.name, "email": stmt.excluded.email},
    )
    async with engine.begin() as conn:
        await conn.execute(stmt)
    return {"count": len(rows)}


@get("/ping")
async def ping() -> dict:
    return {"ok": True}


app = Litestar(route_handlers=[rows, insert_rows, update_rows, upsert_rows, ping])
'''

BLACKSHEEP_CORE_APP = '''
import os

from blacksheep import Application, Request
from sqlalchemy import Column, Integer, MetaData, String, Table, bindparam, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

DSN = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/postgres"
ROWS = int(os.environ.get("BENCH_ROWS", "100"))

metadata = MetaData()
bench_rows = Table(
    "bench_rows", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String),
    Column("email", String),
)

engine = create_async_engine(DSN)

app = Application()


@app.router.get("/rows")
async def rows():
    async with engine.connect() as conn:
        result = await conn.execute(select(bench_rows).limit(ROWS))
        records = result.mappings().all()
    return {"rows": [dict(r) for r in records]}


@app.router.post("/rows")
async def insert_rows(request: Request):
    data = await request.json()
    rows = data["rows"]
    async with engine.begin() as conn:
        await conn.execute(bench_rows.insert(), rows)
    return {"count": len(rows)}


@app.router.put("/rows")
async def update_rows(request: Request):
    data = await request.json()
    rows = data["rows"]
    async with engine.begin() as conn:
        await conn.execute(
            bench_rows.update().where(bench_rows.c.id == bindparam("b_id")),
            [{"b_id": r["id"], "name": r["name"], "email": r["email"]} for r in rows],
        )
    return {"count": len(rows)}


@app.router.post("/rows/upsert")
async def upsert_rows(request: Request):
    data = await request.json()
    rows = data["rows"]
    stmt = pg_insert(bench_rows).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[bench_rows.c.id],
        set_={"name": stmt.excluded.name, "email": stmt.excluded.email},
    )
    async with engine.begin() as conn:
        await conn.execute(stmt)
    return {"count": len(rows)}


@app.router.get("/ping")
async def ping():
    return {"ok": True}
'''

APPS = {
    "ferronit-model": FERRONIT_APP,
    "litestar-msgspec": LITESTAR_MSGSEC_APP,
    "fastapi-orm": FASTAPI_ORM_APP,
    "litestar-orm": LITESTAR_ORM_APP,
    "litestar-asyncpg": LITESTAR_ASYNCPG_APP,
    "blacksheep": BLACKSHEEP_APP,
    "litestar-core": LITESTAR_CORE_APP,
    "blacksheep-core": BLACKSHEEP_CORE_APP,
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


def ab_write(port: int, path: str, method: str, body: str, n: int) -> float:
    """req/s по ApacheBench для POST/PUT с JSON-телом."""
    body_file = HERE / "_bench_write.json"
    body_file.write_text(body)
    cmd = ["ab", "-n", str(n), "-c", "50", "-k", "-T", "application/json"]
    if method == "POST":
        cmd += ["-p", str(body_file)]
    elif method == "PUT":
        cmd += ["-u", str(body_file)]
    cmd.append(f"http://127.0.0.1:{port}{path}")
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300).stdout
    match = re.search(r"Requests per second:\s+([\d.]+)", out)
    if not match:
        raise RuntimeError(f"ab не дал результат для {method} {path}:\n{out[-400:]}")
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


def run_writes() -> None:
    """Замеры записи батчами 1/100/1000 строк: INSERT / UPDATE / UPSERT."""
    ops = {
        "INSERT": ("POST", "/rows", "insert"),
        "UPDATE": ("PUT", "/rows", "update"),
        "UPSERT": ("POST", "/rows/upsert", "upsert"),
    }
    print("\nЗаписи, батч 1/100/1000 строк (granian, 1 воркер, ab -c 50 -k, req/s):")
    for op, (method, path, body_op) in ops.items():
        header = f"{op:>7} {'строк':>6} | " + " | ".join(f"{name:>17}" for name in WRITE_APPS)
        print(header)
        print("-" * len(header))
        for rows in ROWS_LIST:
            procs = {name: start(name, PORTS[name], rows) for name in WRITE_APPS}
            try:
                for name in WRITE_APPS:
                    wait_ready(PORTS[name])
                body = bulk_body(body_op, rows)
                for name in WRITE_APPS:
                    ab_write(PORTS[name], path, method, body, 50)
                best = {name: 0.0 for name in WRITE_APPS}
                for _ in range(RUNS):
                    for name in WRITE_APPS:
                        rps = ab_write(PORTS[name], path, method, body, WRITE_REQUESTS[rows])
                        best[name] = max(best[name], rps)
            finally:
                for proc in procs.values():
                    proc.terminate()
                for proc in procs.values():
                    proc.wait()
            cells = " | ".join(f"{best[name]:>17,.0f}" for name in WRITE_APPS)
            print(f"       {rows:>6} | {cells}")


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

    run_writes()


if __name__ == "__main__":
    main()
