"""Бенч с БД: Ferronit vs FastAPI, HTTP → handler → SQLite → response.

GET /users — SELECT 100 записей + JSON-ответ (чтение)
POST /users — INSERT + ответ (запись)
Прогон через ab (C-клиент) на uvicorn и granian.
"""
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

VENV_PY = Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python"
PORTS = {"ferronit": 8161, "ferronit-core": 8163, "fastapi": 8162}

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


engine = create_async_engine("sqlite+aiosqlite://", echo=False)
factory = async_sessionmaker(engine, expire_on_commit=False)


def init_db():
    import asyncio

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            for i in range(100):
                session.add(User(name=f"user_{i}", email=f"u{i}@example.com"))
            await session.commit()

    asyncio.run(_init())


init_db()
'''

APP_CODE = {
    "ferronit": """
from ferronit import Ferronit
""" + APP_CORE + """

app = Ferronit()


@app.route("/users")
async def list_users(req):
    from sqlalchemy import select

    async with factory() as session:
        result = await session.execute(select(User))
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
""",
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

app = Ferronit()


@app.route("/users")
async def list_users(req):
    async with factory() as session:
        result = await session.execute(select(users))
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
''',
    "fastapi": """
from fastapi import FastAPI
""" + APP_CORE + """

app = FastAPI()


@app.get("/users")
async def list_users():
    from sqlalchemy import select

    async with factory() as session:
        result = await session.execute(select(User))
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
""",
}


def start_server(name: str, port: int, server: str, code: str) -> subprocess.Popen:
    mod_name = name.replace("-", "_")
    app_file = Path(__file__).parent / f"_bench_db_{mod_name}.py"
    app_file.write_text(code)
    if server == "granian":
        cmd = [str(VENV_PY), "-m", "granian", "--interface", "asgi",
               "--host", "127.0.0.1", "--port", str(port), "--workers", "1",
               "--no-ws", "--log-level", "warning", f"_bench_db_{mod_name}:app"]
    else:
        cmd = [str(VENV_PY), "-m", "uvicorn", f"_bench_db_{mod_name}:app",
               "--host", "127.0.0.1", "--port", str(port), "--workers", "1",
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


def ab_bench(url: str, n: int, concurrency: int = 50, method: str = "GET") -> float:
    """Прогон через ab, возвращает req/s."""
    cmd = ["ab", "-n", str(n), "-c", str(concurrency), "-k"]
    if method == "POST":
        body_file = Path(__file__).parent / "_bench_db_post.json"
        body_file.write_text('{"name": "bench", "email": "bench@example.com"}')
        cmd += ["-p", str(body_file), "-T", "application/json"]
    cmd.append(url)
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300).stdout
    m = re.search(r"Requests per second:\s+([\d.]+)", out)
    if not m:
        raise RuntimeError(f"ab failed for {url}:\n{out[-500:]}")
    return float(m.group(1))


def bench_pair(server: str) -> dict:
    procs = {
        name: start_server(name, port, server, APP_CODE[name])
        for name, port in PORTS.items()
    }
    try:
        for name, port in PORTS.items():
            wait_ready(f"http://127.0.0.1:{port}/users")
        out = {}
        for method, path, n in (("GET", "/users", 5000), ("POST", "/users", 3000)):
            out[method] = {}
            for name, port in PORTS.items():
                out[method][name] = ab_bench(f"http://127.0.0.1:{port}{path}", n, method=method)
        return out
    finally:
        for p in procs.values():
            p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


def main() -> None:
    server = sys.argv[1] if len(sys.argv) > 1 else "granian"
    print(f"=== Бенч с БД (SQLite): Ferronit vs FastAPI на {server} ===")
    print("GET /users — SELECT 100 записей, POST /users — INSERT\n")

    res = bench_pair(server)

    print(f"{'операция':10s} {'Ferronit-ORM':>10s} {'Ferronit-Core':>10s} {'FastAPI':>10s}")
    for method in ("GET", "POST"):
        v = res[method]["ferronit"]
        c = res[method]["ferronit-core"]
        f = res[method]["fastapi"]
        print(f"{method:10s} {v:10,.0f} {c:10,.0f} {f:10,.0f}")
        print(f"{'':10s} Core vs ORM: {(c/v-1)*100:+6.0f}%   Ferronit-Core vs FastAPI: {(c/f-1)*100:+6.0f}%")


if __name__ == "__main__":
    main()
