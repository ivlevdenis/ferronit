"""Эксперимент: варианты БД-доступа в HTTP-хендлерах (granian, ab).

1. sqlalchemy-core — текущий CoreRepository-путь (session на запрос)
2. aiosqlite-direct — голый aiosqlite, коннект в глобале
3. sqlite3-thread — sync sqlite3 через asyncio.to_thread
"""
import re
import subprocess
import time
from pathlib import Path

import httpx

VENV_PY = Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python"
PORTS = {"core": 8171, "aiosqlite": 8172, "sqlite3": 8173}

APPS = {
    "core": '''
from ferrox import Ferrox
from sqlalchemy import Column, Integer, MetaData, String, Table, insert, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

metadata = MetaData()
users = Table(
    "users", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String, nullable=False),
    Column("email", String, nullable=False),
)
engine = create_async_engine("sqlite+aiosqlite://", echo=False)
factory = async_sessionmaker(engine, expire_on_commit=False)


def init_db():
    import asyncio
    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
        async with factory() as session:
            for i in range(100):
                await session.execute(insert(users).values(name=f"user_{i}", email=f"u{i}@example.com"))
            await session.commit()
    asyncio.run(_init())


init_db()
app = Ferrox()


@app.route("/users")
async def list_users(req):
    async with factory() as session:
        result = await session.execute(select(users))
        rows = [dict(r) for r in result.mappings().all()]
    return {"users": rows}
''',
    "aiosqlite": '''
from ferrox import Ferrox
import asyncio
import aiosqlite

DB = None


async def init_db():
    global DB
    DB = await aiosqlite.connect(":memory:")
    await DB.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT)")
    await DB.executemany(
        "INSERT INTO users (name, email) VALUES (?, ?)",
        [(f"user_{i}", f"u{i}@example.com") for i in range(100)],
    )
    await DB.commit()


asyncio.run(init_db())
app = Ferrox()


@app.route("/users")
async def list_users(req):
    cur = await DB.execute("SELECT id, name, email FROM users")
    rows = await cur.fetchall()
    return {"users": [{"id": r[0], "name": r[1], "email": r[2]} for r in rows]}
''',
    "sqlite3": '''
from ferrox import Ferrox
import asyncio
import sqlite3

CONN = sqlite3.connect(":memory:", check_same_thread=False)
CONN.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT)")
CONN.executemany(
    "INSERT INTO users (name, email) VALUES (?, ?)",
    [(f"user_{i}", f"u{i}@example.com") for i in range(100)],
)
CONN.commit()

app = Ferrox()


@app.route("/users")
async def list_users(req):
    def _query():
        cur = CONN.execute("SELECT id, name, email FROM users")
        return cur.fetchall()

    rows = await asyncio.to_thread(_query)
    return {"users": [{"id": r[0], "name": r[1], "email": r[2]} for r in rows]}
''',
}


def start_server(name: str, port: int) -> subprocess.Popen:
    app_file = Path(__file__).parent / f"_bench_dbvar_{name}.py"
    app_file.write_text(APPS[name])
    return subprocess.Popen(
        [str(VENV_PY), "-m", "granian", "--interface", "asgi",
         "--host", "127.0.0.1", "--port", str(port), "--workers", "1",
         "--no-ws", "--log-level", "warning", f"_bench_dbvar_{name}:app"],
        cwd=Path(__file__).parent,
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


def ab_bench(url: str, n: int = 5000) -> float:
    out = subprocess.run(
        ["ab", "-n", str(n), "-c", "50", "-k", url],
        capture_output=True, text=True, timeout=300,
    ).stdout
    m = re.search(r"Requests per second:\s+([\d.]+)", out)
    if not m:
        raise RuntimeError(f"ab failed:\n{out[-500:]}")
    return float(m.group(1))


def main():
    procs = {name: start_server(name, port) for name, port in PORTS.items()}
    try:
        for name, port in PORTS.items():
            wait_ready(f"http://127.0.0.1:{port}/users")
        print(f"{'вариант':16s} {'req/s':>10s}")
        for name, port in PORTS.items():
            rps = ab_bench(f"http://127.0.0.1:{port}/users")
            print(f"{name:16s} {rps:10,.0f}")
    finally:
        for p in procs.values():
            p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
