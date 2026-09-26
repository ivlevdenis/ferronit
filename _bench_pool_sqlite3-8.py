
from velox import Velox
import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor

POOL = []
_COUNTER = 0
EXECUTOR = ThreadPoolExecutor(max_workers=8)


def init_db():
    for _ in range(8):
        conn = sqlite3.connect("/tmp/velox_bench_pool.db", check_same_thread=False)
        conn.execute("DROP TABLE IF EXISTS users")
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT)")
        conn.executemany(
            "INSERT INTO users (name, email) VALUES (?, ?)",
            [(f"user_{i}", f"u{i}@example.com") for i in range(100)],
        )
        conn.commit()
        POOL.append(conn)


init_db()
app = Velox()


@app.route("/users")
async def list_users(req):
    global _COUNTER
    conn = POOL[_COUNTER % len(POOL)]
    _COUNTER += 1

    def _query(c):
        cur = c.execute("SELECT id, name, email FROM users")
        return cur.fetchall()

    rows = await asyncio.to_thread(_query, conn)
    return {"users": [{"id": r[0], "name": r[1], "email": r[2]} for r in rows]}
