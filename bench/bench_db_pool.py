"""Эксперимент 2: пул коннектов к ФАЙЛОВОЙ SQLite (in-memory = 1 коннект = сериализация).

1. aiosqlite + пул 8 коннектов (round-robin)
2. sqlite3 sync + пул 8 коннектов + to_thread
3. aiosqlite одиночный (baseline для сравнения)
"""
import re
import subprocess
import time
from pathlib import Path

import httpx

VENV_PY = Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python"
PORTS = {"aiosqlite-1": 8181, "aiosqlite-8": 8182, "sqlite3-8": 8183}
DB_FILE = "/tmp/ferronit_bench_pool.db"

APPS = {
    "aiosqlite-1": f'''
from ferronit import Ferronit
import asyncio
import aiosqlite

DB = None


async def init_db():
    global DB
    DB = await aiosqlite.connect("{DB_FILE}")
    await DB.execute("DROP TABLE IF EXISTS users")
    await DB.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT)")
    await DB.executemany(
        "INSERT INTO users (name, email) VALUES (?, ?)",
        [(f"user_{{i}}", f"u{{i}}@example.com") for i in range(100)],
    )
    await DB.commit()


asyncio.run(init_db())
app = Ferronit()


@app.route("/users")
async def list_users(req):
    cur = await DB.execute("SELECT id, name, email FROM users")
    rows = await cur.fetchall()
    return {{"users": [{{"id": r[0], "name": r[1], "email": r[2]}} for r in rows]}}
''',
    "aiosqlite-8": f'''
from ferronit import Ferronit
import asyncio
import aiosqlite
from itertools import cycle

POOL = []
_COUNTER = 0


async def init_db():
    global POOL
    for _ in range(8):
        db = await aiosqlite.connect("{DB_FILE}")
        await db.execute("DROP TABLE IF EXISTS users")
        await db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT)")
        await db.executemany(
            "INSERT INTO users (name, email) VALUES (?, ?)",
            [(f"user_{{i}}", f"u{{i}}@example.com") for i in range(100)],
        )
        await db.commit()
        POOL.append(db)


asyncio.run(init_db())
app = Ferronit()


@app.route("/users")
async def list_users(req):
    global _COUNTER
    db = POOL[_COUNTER % len(POOL)]
    _COUNTER += 1
    cur = await db.execute("SELECT id, name, email FROM users")
    rows = await cur.fetchall()
    return {{"users": [{{"id": r[0], "name": r[1], "email": r[2]}} for r in rows]}}
''',
    "sqlite3-8": f'''
from ferronit import Ferronit
import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor

POOL = []
_COUNTER = 0
EXECUTOR = ThreadPoolExecutor(max_workers=8)


def init_db():
    for _ in range(8):
        conn = sqlite3.connect("{DB_FILE}", check_same_thread=False)
        conn.execute("DROP TABLE IF EXISTS users")
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT)")
        conn.executemany(
            "INSERT INTO users (name, email) VALUES (?, ?)",
            [(f"user_{{i}}", f"u{{i}}@example.com") for i in range(100)],
        )
        conn.commit()
        POOL.append(conn)


init_db()
app = Ferronit()


@app.route("/users")
async def list_users(req):
    global _COUNTER
    conn = POOL[_COUNTER % len(POOL)]
    _COUNTER += 1

    def _query(c):
        cur = c.execute("SELECT id, name, email FROM users")
        return cur.fetchall()

    rows = await asyncio.to_thread(_query, conn)
    return {{"users": [{{"id": r[0], "name": r[1], "email": r[2]}} for r in rows]}}
''',
}


def start_server(name: str, port: int) -> subprocess.Popen:
    app_file = Path(__file__).parent / f"_bench_pool_{name}.py"
    app_file.write_text(APPS[name])
    return subprocess.Popen(
        [str(VENV_PY), "-m", "granian", "--interface", "asgi",
         "--host", "127.0.0.1", "--port", str(port), "--workers", "1",
         "--no-ws", "--log-level", "warning", f"_bench_pool_{name}:app"],
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
    Path(DB_FILE).unlink(missing_ok=True)
    procs = {name: start_server(name, port) for name, port in PORTS.items()}
    try:
        for name, port in PORTS.items():
            wait_ready(f"http://127.0.0.1:{port}/users")
        print(f"{'вариант':14s} {'req/s':>10s}")
        for name, port in PORTS.items():
            rps = ab_bench(f"http://127.0.0.1:{port}/users")
            print(f"{name:14s} {rps:10,.0f}")
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
