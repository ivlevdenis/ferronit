"""Ferronit-сравнение: тот же сервис (6 эндпоинтов на `bench_rows`) на Tornado.

Идиоматичный Tornado: `tornado.web.Application` + `RequestHandler`, `async def`-хендлеры,
сырой asyncpg (своего ORM/валидации/DI у Tornado нет). Сервер — собственный `HTTPServer`
Tornado на одном IOLoop (не ASGI → не granian), один процесс.

Запуск: `.venv/bin/python bench/tornado_bench/app.py` (слушает :8246).
"""

import asyncio
import json

import asyncpg
import tornado.web

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"

_pool = None
_lock = None


async def get_pool():
    """Ленивый пул asyncpg (создаётся в цикле IOLoop по первому запросу)."""
    global _pool, _lock
    if _lock is None:
        _lock = asyncio.Lock()
    if _pool is None:
        async with _lock:
            if _pool is None:
                _pool = await asyncpg.create_pool(DSN, min_size=1, max_size=16)
    return _pool


class PingHandler(tornado.web.RequestHandler):
    async def get(self):
        self.write({"ok": True})


class RowsHandler(tornado.web.RequestHandler):
    async def get(self):
        limit = int(self.get_query_argument("limit", "100"))
        pool = await get_pool()
        async with pool.acquire() as conn:
            records = await conn.fetch(
                "SELECT id, name, email FROM bench_rows LIMIT $1", limit
            )
        self.write({"rows": [dict(r) for r in records]})

    async def post(self):
        data = json.loads(self.request.body)
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO bench_rows (name, email) VALUES ($1, $2) RETURNING id",
                data["name"], data["email"],
            )
        self.write({"id": row["id"]})


class RowHandler(tornado.web.RequestHandler):
    async def put(self, row_id):
        data = json.loads(self.request.body)
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE bench_rows SET name = $1, email = $2 WHERE id = $3 RETURNING id",
                data["name"], data["email"], int(row_id),
            )
        self.write({"id": row["id"] if row is not None else None})

    async def delete(self, row_id):
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM bench_rows WHERE id = $1", int(row_id))
        self.write({"ok": True})


class UpsertHandler(tornado.web.RequestHandler):
    async def post(self):
        data = json.loads(self.request.body)
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO bench_rows (id, name, email) VALUES ($1, $2, $3) "
                "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, "
                "email = EXCLUDED.email RETURNING id",
                data["id"], data["name"], data["email"],
            )
        self.write({"id": row["id"]})


def make_app():
    return tornado.web.Application([
        (r"/ping", PingHandler),
        (r"/rows", RowsHandler),
        (r"/rows/upsert", UpsertHandler),
        (r"/rows/([0-9]+)", RowHandler),
    ])


async def main():
    make_app().listen(8246)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
