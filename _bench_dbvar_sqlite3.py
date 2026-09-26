
from velox import Velox
import asyncio
import sqlite3

CONN = sqlite3.connect(":memory:", check_same_thread=False)
CONN.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT)")
CONN.executemany(
    "INSERT INTO users (name, email) VALUES (?, ?)",
    [(f"user_{i}", f"u{i}@example.com") for i in range(100)],
)
CONN.commit()

app = Velox()


@app.route("/users")
async def list_users(req):
    def _query():
        cur = CONN.execute("SELECT id, name, email FROM users")
        return cur.fetchall()

    rows = await asyncio.to_thread(_query)
    return {"users": [{"id": r[0], "name": r[1], "email": r[2]} for r in rows]}
