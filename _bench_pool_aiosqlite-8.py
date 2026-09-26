
from velox import Velox
import asyncio
import aiosqlite
from itertools import cycle

POOL = []
_COUNTER = 0


async def init_db():
    global POOL
    for _ in range(8):
        db = await aiosqlite.connect("/tmp/velox_bench_pool.db")
        await db.execute("DROP TABLE IF EXISTS users")
        await db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT)")
        await db.executemany(
            "INSERT INTO users (name, email) VALUES (?, ?)",
            [(f"user_{i}", f"u{i}@example.com") for i in range(100)],
        )
        await db.commit()
        POOL.append(db)


asyncio.run(init_db())
app = Velox()


@app.route("/users")
async def list_users(req):
    global _COUNTER
    db = POOL[_COUNTER % len(POOL)]
    _COUNTER += 1
    cur = await db.execute("SELECT id, name, email FROM users")
    rows = await cur.fetchall()
    return {"users": [{"id": r[0], "name": r[1], "email": r[2]} for r in rows]}
