
from velox import Velox
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
app = Velox()


@app.route("/users")
async def list_users(req):
    cur = await DB.execute("SELECT id, name, email FROM users")
    rows = await cur.fetchall()
    return {"users": [{"id": r[0], "name": r[1], "email": r[2]} for r in rows]}
