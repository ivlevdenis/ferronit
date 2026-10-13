"""Litestar-реализация того же API, что и `django_bench` / `ferronit_service`.

Сравнение «один сервис, несколько реализаций» — в `docs/vs-litestar.md`: этот файл —
вариант LS1 (идиоматичный Litestar: msgspec-модели тела, DI через `Provide`,
`Parameter` с констрейнтами, OpenAPI/Swagger из коробки). Стендовая версия для
замеров (сырой asyncpg + msgspec, без DI и DTO) — строка `LITESTAR_APP` в
`bench/api_bench.py`.

Эндпоинты:

    GET    /ping              -> {"ok": true}
    GET    /rows?limit=N      -> {"rows": [...]}
    POST   /rows              -> {"id": N}
    PUT    /rows/{id}         -> {"id": N}
    DELETE /rows/{id}         -> {"ok": true}
    POST   /rows/upsert       -> {"id": N}

Запуск (нужен PostgreSQL на :5432 и таблица bench_rows):

    .venv/bin/python -m uvicorn app:app --port 8340 --app-dir bench/litestar_bench
    # схема из коробки: /schema/openapi.json, /schema/swagger
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
import msgspec
from litestar import Litestar, delete, get, post, put
from litestar.di import Provide
from litestar.params import Parameter

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"


class RowIn(msgspec.Struct):
    """Тело запроса: создание и обновление (Litestar принимает msgspec как есть)."""

    name: str
    email: str


class RowUpsert(msgspec.Struct):
    """Тело upsert — тот же набор плюс id."""

    id: int
    name: str
    email: str


class RowsRepo:
    """Репозиторий на сыром asyncpg: явный SQL и bound-параметры.

    Пул создаётся лениво (asyncpg привязан к циклу событий сервера), а на
    shutdown приложения закрывается через `lifespan`.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()

    async def _pool_or_create(self) -> asyncpg.Pool:
        if self._pool is None:
            async with self._lock:
                if self._pool is None:
                    self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=4)
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def list(self, limit: int) -> list[dict]:
        pool = await self._pool_or_create()
        async with pool.acquire() as conn:
            records = await conn.fetch(
                "SELECT id, name, email FROM bench_rows LIMIT $1", limit
            )
        return [dict(r) for r in records]

    async def insert(self, name: str, email: str) -> int:
        pool = await self._pool_or_create()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO bench_rows (name, email) VALUES ($1, $2) RETURNING id", name, email
            )
        return row["id"]

    async def update(self, row_id: int, name: str, email: str) -> int | None:
        pool = await self._pool_or_create()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE bench_rows SET name = $1, email = $2 WHERE id = $3 RETURNING id",
                name, email, row_id,
            )
        return row["id"] if row is not None else None

    async def delete(self, row_id: int) -> None:
        pool = await self._pool_or_create()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM bench_rows WHERE id = $1", row_id)

    async def upsert(self, row_id: int, name: str, email: str) -> int:
        pool = await self._pool_or_create()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO bench_rows (id, name, email) VALUES ($1, $2, $3) "
                "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, email = EXCLUDED.email "
                "RETURNING id",
                row_id, name, email,
            )
        return row["id"]


_repo = RowsRepo(DSN)


async def provide_repo() -> RowsRepo:
    """DI-провайдер: Litestar передаёт результат в параметр `repo`."""
    return _repo


@asynccontextmanager
async def lifespan(_: Litestar) -> AsyncIterator[None]:
    """Пул закрывается на shutdown приложения."""
    yield
    await _repo.close()


@get("/ping")
async def ping() -> dict:
    return {"ok": True}


@get("/rows")
async def rows(repo: RowsRepo, limit: int = Parameter(default=100, ge=1, le=1000)) -> dict:
    return {"rows": await repo.list(limit)}


@post("/rows")
async def insert(data: RowIn, repo: RowsRepo) -> dict:
    return {"id": await repo.insert(data.name, data.email)}


@put("/rows/{row_id:int}")
async def update(row_id: int, data: RowIn, repo: RowsRepo) -> dict:
    return {"id": await repo.update(row_id, data.name, data.email)}


@delete("/rows/{row_id:int}", status_code=200)
async def remove(row_id: int, repo: RowsRepo) -> dict:
    await repo.delete(row_id)
    return {"ok": True}


@post("/rows/upsert")
async def upsert(data: RowUpsert, repo: RowsRepo) -> dict:
    return {"id": await repo.upsert(data.id, data.name, data.email)}


app = Litestar(
    route_handlers=[ping, rows, insert, update, remove, upsert],
    dependencies={"repo": Provide(provide_repo)},
    lifespan=[lifespan],
)
