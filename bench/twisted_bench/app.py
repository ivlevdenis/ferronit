"""Ferronit-сравнение: тот же сервис (6 эндпоинтов на `bench_rows`) на Twisted.

Идиоматичный Twisted: `twisted.web.server.Site` + `Resource`, сырой asyncpg поверх
`AsyncioSelectorReactor` (реактор крутит asyncio-цикл, coroutine в хендлер идёт через
`defer.ensureDeferred`). Свой HTTP-сервер (`reactor.listenTCP`), не ASGI → не granian,
один процесс.

Запуск: `.venv/bin/python bench/twisted_bench/app.py` (слушает :8247).
"""

import asyncio
import json

import asyncpg
from twisted.internet import asyncioreactor

asyncioreactor.install()
from twisted.internet import reactor
from twisted.internet.defer import Deferred
from twisted.web import pages, resource, server
from twisted.web.server import NOT_DONE_YET

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"

_pool = None
_lock = None


async def get_pool():
    """Ленивый пул asyncpg (создаётся в asyncio-цикле реактора по первому запросу)."""
    global _pool, _lock
    if _lock is None:
        _lock = asyncio.Lock()
    if _pool is None:
        async with _lock:
            if _pool is None:
                _pool = await asyncpg.create_pool(DSN, min_size=1, max_size=16)
    return _pool


class BaseResource(resource.Resource):
    """Обёртка: coroutine хендлера в Deferred, ошибка → 500 + finish."""

    def _run(self, coro, request):
        d = Deferred.fromFuture(asyncio.ensure_future(coro))
        d.addErrback(self._error, request)
        return NOT_DONE_YET

    def _error(self, failure, request):
        request.setResponseCode(500)
        request.write(b"internal error")
        request.finish()

    def _json(self, request, obj):
        request.setHeader(b"content-type", b"application/json")
        request.write(json.dumps(obj).encode())
        request.finish()


class PingResource(BaseResource):
    isLeaf = True

    def render_GET(self, request):
        return self._run(self._get(request), request)

    async def _get(self, request):
        self._json(request, {"ok": True})


class RowsResource(BaseResource):
    def getChild(self, path, request):
        if path == b"upsert":
            return UpsertResource()
        if path.isdigit():
            return RowResource(int(path))
        return pages.notFound()

    def render_GET(self, request):
        return self._run(self._get(request), request)

    async def _get(self, request):
        limit = int(request.args.get(b"limit", [b"100"])[0])
        pool = await get_pool()
        async with pool.acquire() as conn:
            records = await conn.fetch(
                "SELECT id, name, email FROM bench_rows LIMIT $1", limit
            )
        self._json(request, {"rows": [dict(r) for r in records]})

    def render_POST(self, request):
        return self._run(self._post(request), request)

    async def _post(self, request):
        data = json.loads(request.content.read())
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO bench_rows (name, email) VALUES ($1, $2) RETURNING id",
                data["name"], data["email"],
            )
        self._json(request, {"id": row["id"]})


class RowResource(BaseResource):
    isLeaf = True

    def __init__(self, row_id):
        super().__init__()
        self._row_id = row_id

    def render_PUT(self, request):
        return self._run(self._put(request), request)

    async def _put(self, request):
        data = json.loads(request.content.read())
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE bench_rows SET name = $1, email = $2 WHERE id = $3 RETURNING id",
                data["name"], data["email"], self._row_id,
            )
        self._json(request, {"id": row["id"] if row is not None else None})

    def render_DELETE(self, request):
        return self._run(self._delete(request), request)

    async def _delete(self, request):
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM bench_rows WHERE id = $1", self._row_id)
        self._json(request, {"ok": True})


class UpsertResource(BaseResource):
    isLeaf = True

    def render_POST(self, request):
        return self._run(self._post(request), request)

    async def _post(self, request):
        data = json.loads(request.content.read())
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO bench_rows (id, name, email) VALUES ($1, $2, $3) "
                "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, "
                "email = EXCLUDED.email RETURNING id",
                data["id"], data["name"], data["email"],
            )
        self._json(request, {"id": row["id"]})


def make_root():
    root = resource.Resource()
    root.putChild(b"ping", PingResource())
    root.putChild(b"rows", RowsResource())
    return root


def main():
    site = server.Site(make_root())
    reactor.listenTCP(8247, site)
    reactor.run()


if __name__ == "__main__":
    main()
