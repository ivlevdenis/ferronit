"""Бенч с реальным PostgreSQL (Docker): Ferronit (Core/ORM) vs FastAPI.

Требует: docker run -d --name ferronit-pg -e POSTGRES_PASSWORD=postgres \
    -e POSTGRES_USER=postgres -e POSTGRES_DB=postgres -p 5432:5432 postgres:latest
"""
import asyncio
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

VENV_PY = Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python"
DB_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/postgres"
PORTS = {"ferronit-asyncpg": 8194, "ferronit-rawrepo": 8195, "ferronit-core": 8191, "ferronit-orm": 8192, "fastapi": 8193}
# Воркеров на приложение: 1 по умолчанию. BENCH_WORKERS=4 проверяет, упирается ли
# результат в Python-слой приложения (тогда масштабируется) или в базу (тогда нет).
WORKERS = int(os.environ.get("BENCH_WORKERS", "1"))

APP_CORE = '''
from sqlalchemy import Column, Integer, String
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False)


engine = create_async_engine("__DB_URL__", echo=False)
factory = async_sessionmaker(engine, expire_on_commit=False)


def init_db():
    import asyncio

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            from sqlalchemy import func, select

            count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
            if count == 0:
                for i in range(100):
                    session.add(User(name=f"user_{i}", email=f"u{i}@example.com"))
                await session.commit()
            await engine.dispose()

    asyncio.run(_init())


init_db()
'''

APPS = {
    "ferronit-rawrepo": '''
import asyncio

from ferronit import Ferronit
from ferronit.contrib.rawdb import RawUnitOfWork, create_raw_pool

DSN = "__DB_URL__".replace("postgresql+asyncpg://", "postgresql://")
TABLE = "users"

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


def init_db() -> None:
    """Схему и данные готовит Python-стенд; здесь только проверяем доступность."""
    async def _check() -> None:
        import asyncpg
        conn = await asyncpg.connect(DSN)
        try:
            await conn.fetchval("SELECT count(*) FROM users")
        finally:
            await conn.close()

    asyncio.run(_check())


init_db()

app = Ferronit()


@app.route("/users")
async def list_users(req):
    async with RawUnitOfWork(await get_pool()) as uow:
        rows = await uow[TABLE].list(limit=100)
    return {"users": rows}


@app.route("/users", methods=["POST"])
async def create_user(req):
    body = await req.json()
    async with RawUnitOfWork(await get_pool()) as uow:
        row = await uow[TABLE].save({"name": body["name"], "email": body["email"]})
    return {"id": row["id"], "name": row["name"]}


@app.route("/ping")
async def ping(req):
    """Тот же сервер и тот же путь обработки, но без обращения к базе."""
    return {"ok": True}
''',
    "ferronit-asyncpg": '''
import asyncio

import asyncpg
from ferronit import Ferronit

DSN = "__DB_URL__".replace("postgresql+asyncpg://", "postgresql://")


def init_db() -> None:
    async def _init() -> None:
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS users ("
                "id serial PRIMARY KEY, name text NOT NULL, email text NOT NULL)"
            )
            count = await conn.fetchval("SELECT count(*) FROM users")
            if not count:
                await conn.execute(
                    "INSERT INTO users (name, email) SELECT 'user_' || g, 'u' || g || '@example.com' "
                    "FROM generate_series(0, 99) AS g"
                )
        finally:
            await conn.close()

    asyncio.run(_init())


init_db()

app = Ferronit()
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


@app.route("/users")
async def list_users(req):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, name, email FROM users LIMIT 100")
    return {"users": [dict(r) for r in rows]}


@app.route("/users", methods=["POST"])
async def create_user(req):
    body = await req.json()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO users (name, email) VALUES ($1, $2) RETURNING id, name",
            body["name"],
            body["email"],
        )
    return {"id": row["id"], "name": row["name"]}


@app.route("/ping")
async def ping(req):
    """Тот же сервер и тот же путь обработки, но без обращения к базе."""
    return {"ok": True}
''',
    "ferronit-core": '''
from ferronit import Ferronit
from sqlalchemy import Column, Integer, MetaData, String, Table, insert, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

metadata = MetaData()
users = Table(
    "users", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String, nullable=False),
    Column("email", String, nullable=False),
)

engine = create_async_engine("__DB_URL__", echo=False)
factory = async_sessionmaker(engine, expire_on_commit=False)


def init_db():
    import asyncio

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
        async with factory() as session:
            from sqlalchemy import func, select

            count = (await session.execute(select(func.count()).select_from(users))).scalar_one()
            if count == 0:
                for i in range(100):
                    await session.execute(insert(users).values(name=f"user_{i}", email=f"u{i}@example.com"))
                await session.commit()
            await engine.dispose()

    asyncio.run(_init())


init_db()

app = Ferronit()


@app.route("/users")
async def list_users(req):
    async with factory() as session:
        result = await session.execute(select(users).limit(100))
        rows = [dict(r) for r in result.mappings().all()]
    return {"users": rows}


@app.route("/users", methods=["POST"])
async def create_user(req):
    body = await req.json()
    async with factory() as session:
        result = await session.execute(
            insert(users).values(name=body["name"], email=body["email"])
        )
        row_id = result.inserted_primary_key[0]
        await session.commit()
    return {"id": row_id, "name": body["name"]}


@app.route("/ping")
async def ping(req):
    """Тот же сервер и тот же путь обработки, но без обращения к базе."""
    return {"ok": True}
''',
    "ferronit-orm": '''
from ferronit import Ferronit
''' + APP_CORE + '''

app = Ferronit()


@app.route("/users")
async def list_users(req):
    from sqlalchemy import select

    async with factory() as session:
        result = await session.execute(select(User).limit(100))
        users = result.scalars().all()
    return {
        "users": [
            {"id": u.id, "name": u.name, "email": u.email} for u in users
        ]
    }


@app.route("/users", methods=["POST"])
async def create_user(req):
    body = await req.json()
    async with factory() as session:
        user = User(name=body["name"], email=body["email"])
        session.add(user)
        await session.commit()
    return {"id": user.id, "name": user.name}


@app.route("/ping")
async def ping(req):
    """Тот же сервер и тот же путь обработки, но без обращения к базе."""
    return {"ok": True}
''',
    "fastapi": '''
from fastapi import FastAPI
''' + APP_CORE + '''

app = FastAPI()


@app.get("/users")
async def list_users():
    from sqlalchemy import select

    async with factory() as session:
        result = await session.execute(select(User).limit(100))
        users = result.scalars().all()
    return {
        "users": [
            {"id": u.id, "name": u.name, "email": u.email} for u in users
        ]
    }


@app.post("/users")
async def create_user(body: dict):
    async with factory() as session:
        user = User(name=body["name"], email=body["email"])
        session.add(user)
        await session.commit()
    return {"id": user.id, "name": user.name}


@app.get("/ping")
async def ping():
    """Тот же сервер и тот же путь обработки, но без обращения к базе."""
    return {"ok": True}
''',
}


def start_server(name: str, port: int, server: str) -> subprocess.Popen:
    mod_name = name.replace("-", "_")
    app_file = Path(__file__).parent / f"_bench_pg_{mod_name}.py"
    app_file.write_text(APPS[name].replace("__DB_URL__", DB_URL))
    if server == "granian":
        cmd = [str(VENV_PY), "-m", "granian", "--interface", "asgi",
               "--host", "127.0.0.1", "--port", str(port), "--workers", str(WORKERS),
               "--no-ws", "--log-level", "warning", f"_bench_pg_{mod_name}:app"]
    else:
        cmd = [str(VENV_PY), "-m", "uvicorn", f"_bench_pg_{mod_name}:app",
               "--host", "127.0.0.1", "--port", str(port), "--workers", str(WORKERS),
               "--log-level", "warning"]
    return subprocess.Popen(
        cmd, cwd=Path(__file__).parent,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def wait_ready(url: str, timeout: float = 20.0) -> None:
    t0 = time.monotonic()
    with httpx.Client(trust_env=False) as c:
        while time.monotonic() - t0 < timeout:
            try:
                if c.get(url, timeout=2).status_code == 200:
                    return
            except Exception:
                time.sleep(0.2)
    raise TimeoutError(f"{url} не поднялся")


def ab_bench(url: str, n: int, method: str = "GET") -> float:
    cmd = ["ab", "-n", str(n), "-c", "50", "-k"]
    if method == "POST":
        body_file = Path(__file__).parent / "_bench_pg_post.json"
        body_file.write_text('{"name": "bench", "email": "bench@example.com"}')
        cmd += ["-p", str(body_file), "-T", "application/json"]
    cmd.append(url)
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300).stdout
    m = re.search(r"Requests per second:\s+([\d.]+)", out)
    if not m:
        raise RuntimeError(f"ab failed for {url}:\n{out[-500:]}")
    return float(m.group(1))


def bench_pair(server: str) -> dict:
    procs = {name: start_server(name, port, server) for name, port in PORTS.items()}
    try:
        for name, port in PORTS.items():
            wait_ready(f"http://127.0.0.1:{port}/users")
        out = {}
        for method, n in (("GET", 5000), ("POST", 3000)):
            out[method] = {}
            for name, port in PORTS.items():
                out[method][name] = ab_bench(f"http://127.0.0.1:{port}/users", n, method=method)
        # контрольный маршрут: тот же сервер, но без обращения к базе
        out["ping"] = {
            name: ab_bench(f"http://127.0.0.1:{port}/ping", 10000)
            for name, port in PORTS.items()
        }
        return out
    finally:
        for p in procs.values():
            p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


def db_version() -> str:
    """Версия сервера из самого сервера — чтобы в отчёте не стояла догадка."""
    async def _get() -> str:
        import asyncpg

        conn = await asyncpg.connect(DB_URL.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            return await conn.fetchval("SHOW server_version")
        finally:
            await conn.close()

    return asyncio.run(_get())


def main() -> None:
    server = sys.argv[1] if len(sys.argv) > 1 else "granian"
    print(f"=== Бенч с PostgreSQL {db_version()} (Docker): Ferronit vs FastAPI на {server}, воркеров: {WORKERS} ===")
    print("GET /users — SELECT 100 записей, POST /users — INSERT, GET /ping — без базы\n")

    res = bench_pair(server)

    print(
        f"{'операция':14s} {'Ferronit+asyncpg':>13s} {'Ferronit+RawRepo':>13s} {'Ferronit-Core':>11s}"
        f" {'Ferronit-ORM':>10s} {'FastAPI':>9s}"
    )
    for label, key in (("GET /users", "GET"), ("POST /users", "POST"), ("GET /ping", "ping")):
        a = res[key]["ferronit-asyncpg"]
        r = res[key]["ferronit-rawrepo"]
        c = res[key]["ferronit-core"]
        o = res[key]["ferronit-orm"]
        f = res[key]["fastapi"]
        print(f"{label:14s} {a:13,.0f} {r:13,.0f} {c:11,.0f} {o:10,.0f} {f:9,.0f}")
        print(
            f"{'':14s} asyncpg vs RawRepo: {(a / r - 1) * 100:+6.0f}%   "
            f"RawRepo vs Core: {(r / c - 1) * 100:+6.0f}%   Core vs ORM: {(c / o - 1) * 100:+6.0f}%"
        )


if __name__ == "__main__":
    main()
