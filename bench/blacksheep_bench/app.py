"""BlackSheep-реализация того же API, что и `django_bench` / `ferronit_service`.

Сравнение «один сервис, несколько реализаций» — в `docs/vs-blacksheep.md`: этот
файл — вариант BS1 (идиоматичный BlackSheep: DAO-сервис в DI-контейнере
`app.services`, `FromQuery` для query-параметра, dataclass-модели тела, OpenAPI
через `APIDocsHandler`). Стендовая версия для замеров (сырой asyncpg + msgspec,
без bindings и DI) — строка `BLACKSHEEP_APP` в `bench/api_bench.py`.

Замечание по версиям: на связке blacksheep 2.6.4 + CPython 3.14.4 аннотации
`From*[T]` (например `limit: FromQuery[int]`, `body: FromJSON[RowIn]`,
`repo: FromServices[Repo]`) не резолвятся — параметр приходит в хендлер объектом
bindings. Работают: тип сервиса как аннотация (`repo: RowsRepo`), простой тип для
пути (`row_id: int`) и `FromQuery(...)`/простой тип для query. Проверено прогоном.

Эндпоинты:

    GET    /ping              -> {"ok": true}
    GET    /rows?limit=N      -> {"rows": [...]}
    POST   /rows              -> {"id": N}
    PUT    /rows/{id}         -> {"id": N}
    DELETE /rows/{id}         -> {"ok": true}
    POST   /rows/upsert       -> {"id": N}

Запуск (нужен PostgreSQL на :5432 и таблица bench_rows):

    .venv/bin/python -m uvicorn app:app --port 8324 --app-dir bench/blacksheep_bench
    # схема из коробки: /docs, /openapi.json
"""

import asyncio
from dataclasses import dataclass

import asyncpg
from blacksheep import Application, FromQuery
from blacksheep.server.openapi.v3 import OpenAPIHandler
from openapidocs.v3 import Info

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"


@dataclass
class RowIn:
    """Тело запроса: создание и обновление."""

    name: str
    email: str


@dataclass
class RowUpsert:
    """Тело upsert — тот же набор плюс id."""

    id: int
    name: str
    email: str


class RowsRepo:
    """Репозиторий на сыром asyncpg: явный SQL и bound-параметры.

    Пул создаётся лениво внутри репозитория (asyncpg привязан к циклу событий
    сервера, поэтому делать это на импорте нельзя), а сам репозиторий — обычный
    сервис DI-контейнера, зарегистрированный до старта приложения.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()

    async def _acquire(self) -> asyncpg.Pool:
        """Ленивая инициализация пула: один раз, под локом, в цикле событий."""
        if self._pool is None:
            async with self._lock:
                if self._pool is None:
                    self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=4)
        return self._pool

    async def close(self) -> None:
        """Закрыть пул на shutdown приложения (если он был создан)."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def list(self, limit: int) -> list[dict]:
        pool = await self._acquire()
        async with pool.acquire() as conn:
            records = await conn.fetch(
                "SELECT id, name, email FROM bench_rows LIMIT $1", limit
            )
        return [dict(r) for r in records]

    async def insert(self, name: str, email: str) -> int:
        pool = await self._acquire()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO bench_rows (name, email) VALUES ($1, $2) RETURNING id", name, email
            )
        return row["id"]

    async def update(self, row_id: int, name: str, email: str) -> int | None:
        pool = await self._acquire()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE bench_rows SET name = $1, email = $2 WHERE id = $3 RETURNING id",
                name, email, row_id,
            )
        return row["id"] if row is not None else None

    async def delete(self, row_id: int) -> None:
        pool = await self._acquire()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM bench_rows WHERE id = $1", row_id)

    async def upsert(self, row_id: int, name: str, email: str) -> int:
        pool = await self._acquire()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO bench_rows (id, name, email) VALUES ($1, $2, $3) "
                "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, email = EXCLUDED.email "
                "RETURNING id",
                row_id, name, email,
            )
        return row["id"]


app = Application()
app.services.add_instance(RowsRepo(DSN))          # сервис виден DI-контейнеру
docs = OpenAPIHandler(info=Info(title="bench", version="0.1.0"), ui_path="/docs")
docs.bind_app(app)


@app.on_stop
async def stop() -> None:
    """Закрыть пул при остановке приложения."""
    await app.services.resolve(RowsRepo).close()


@app.router.get("/ping")
async def ping() -> dict[str, bool]:
    return {"ok": True}


@app.router.get("/rows")
async def rows(repo: RowsRepo, limit: int = FromQuery(100)) -> dict:
    return {"rows": await repo.list(limit)}


@app.router.post("/rows")
async def insert(body: RowIn, repo: RowsRepo) -> dict[str, int]:
    return {"id": await repo.insert(body.name, body.email)}


@app.router.put("/rows/{row_id}")
async def update(
    row_id: int, body: RowIn, repo: RowsRepo
) -> dict[str, int | None]:
    return {"id": await repo.update(row_id, body.name, body.email)}


@app.router.delete("/rows/{row_id}")
async def delete(row_id: int, repo: RowsRepo) -> dict[str, bool]:
    await repo.delete(row_id)
    return {"ok": True}


@app.router.post("/rows/upsert")
async def upsert(body: RowUpsert, repo: RowsRepo) -> dict[str, int]:
    return {"id": await repo.upsert(body.id, body.name, body.email)}
