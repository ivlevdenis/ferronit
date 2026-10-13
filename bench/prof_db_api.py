"""Профиль Ferronit GET /rows?limit=N — где накладные на малых лимитах (1–50).

Лестница слоёв (in-process, прямой вызов ASGI): /ping (без базы) → сырой asyncpg
→ полный /rows (UoW + Repo + rawmodel), плюс cProfile по категориям.
"""
import asyncio
import cProfile
import time
from typing import Annotated

import asyncpg

from ferronit import Ferronit
from ferronit.contrib.rawdb import Pool, create_pool
from ferronit.contrib.rawmodel import Model
from ferronit.di import Container

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
ShopPool = Annotated[Pool, "shop"]


class BenchRow(Model):
    __table__ = "bench_rows"
    id: int = 0
    name: str = ""
    email: str = ""


class Repo:
    def __init__(self, pool: ShopPool):
        self._pool = pool

    async def list(self, limit: int):
        async with self._pool.uow(readonly=True) as uow:
            return await uow.model(BenchRow).list(limit=limit)


container = Container()
app = Ferronit(container=container)
container.factory(Repo)


@app.route("/rows")
async def rows(repo: Repo, limit: int = 100):
    return {"rows": await repo.list(limit)}


@app.route("/ping")
async def ping():
    return {"ok": True}


def make_scope(path: str, query: bytes):
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": query,
        "headers": [(b"host", b"t")],
        "scheme": "http",
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8000),
        "route_params": {},
    }


async def call(path: str, query: bytes = b""):
    scope = make_scope(path, query)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        pass

    await app(scope, receive, send)


_pg_pool = None


async def raw_fetch(limit: int):
    global _pg_pool
    if _pg_pool is None:
        _pg_pool = await asyncpg.create_pool(DSN, min_size=1, max_size=16)
    async with _pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, email FROM bench_rows LIMIT $1", limit
        )
    return [dict(r) for r in rows]


async def time_it(fn, n=500):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        await fn()
        ts.append((time.perf_counter() - t0) * 1e6)
    ts.sort()
    return ts[len(ts) // 2]


async def main():
    pool = await create_pool(DSN, min_size=1, max_size=16)
    container.singleton(ShopPool, pool)
    await call("/rows", b"limit=1")  # прогрев: DI, pool, кэш SQL, lifespan flush

    print("== чистое время, медиана (мкс), in-process, без cProfile ==")
    t_ping = await time_it(lambda: call("/ping"))
    print(f"{'/ping (голый путь)':32s} {t_ping:8.1f}")
    for limit in (1, 20, 50, 100, 1000):
        q = f"limit={limit}".encode()
        t_full = await time_it(lambda: call("/rows", q))
        t_raw = await time_it(lambda: raw_fetch(limit))
        print(
            f"{'  /rows?limit=' + str(limit):32s} full={t_full:8.1f}  "
            f"raw_asyncpg={t_raw:8.1f}  фреймворк+={t_full - t_raw:+8.1f}"
        )

    # cProfile: доли по категориям на limit=50
    pr = cProfile.Profile()
    pr.enable()
    for _ in range(2000):
        await call("/rows", b"limit=50")
    pr.disable()
    pr.create_stats()
    stats = pr.stats
    cats = {
        "ferronit-ядро (app/response/request)": ("ferronit/core",),
        "binding": ("ferronit/binding",),
        "di": ("ferronit/di",),
        "rawmodel (маппинг)": ("ferronit/contrib/rawmodel",),
        "rawdb (uow/pool)": ("ferronit/contrib/rawdb",),
        "asyncpg (драйвер)": ("asyncpg",),
        "msgspec (энкодер)": ("msgspec",),
        "хендлер + bench": ("prof_db_api",),
    }
    totals = {k: 0.0 for k in cats}
    other = 0.0
    for fn, (_cc, _nc, tt, _ct, _callers) in stats.items():
        fname = fn[0]
        hit = False
        for k, prefixes in cats.items():
            if any(p in fname for p in prefixes):
                totals[k] += tt
                hit = True
                break
        if not hit:
            other += tt
    total = sum(totals.values()) + other
    print("\n== cProfile /rows?limit=50 — доли tottime, % ==")
    for k, v in sorted(totals.items(), key=lambda x: -x[1]):
        print(f"  {k:34s} {v / total * 100:5.1f}%")
    print(f"  {'прочее (asyncio/select/epoll)':34s} {other / total * 100:5.1f}%")

    await pool.close()


asyncio.run(main())
