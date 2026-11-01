"""Реалистичный API-бенч: ping + строки из БД с query-параметром + одиночные записи.

Пять Python-фреймворков (Ferronit, BlackSheep, Litestar, FastAPI, Django), четыре
Go-стека (`bench/go_pg_orm`: pgx, sqlx, GORM, ent) и два Rust-фреймворка
(`bench/rust_http`: Axum, Actix Web) в **одной сессии**: одинаковые
эндпоинты и одна и та же 6-колоночная таблица `bench_rows`, каждый — своим
идиоматичным путём (Ferronit=rawmodel, BlackSheep/Litestar=asyncpg+msgspec,
FastAPI=SQLAlchemy ORM, Django=Django ORM, Go=net/http + свой слой доступа).

Замеры идут **раундами по кругу** (все приложения по очереди, `BENCH_RUNS` раундов,
берётся лучшее): дрейф машины внутри раунда затрагивает всех одинаково, поэтому
соотношения Ferronit↔Go↔остальные устойчивы, а не сняты в разных сессиях.

Эндпоинты:
    GET  /ping              -> {"ok": true}
    GET  /rows?limit=N      -> N строк SELECT id, name, email, created_at, birthday, ttl LIMIT $1 (N из query)
    POST /rows              -> INSERT одной строки (name, email + фиксированные даты/время)
    PUT  /rows/{id}         -> UPDATE одной строки {name, email}
    DELETE /rows/{id}       -> DELETE одной строки
    POST /rows/upsert       -> UPSERT одной строки {id, name, email}

Число строк (1/20/50/100/1000) задаётся query-параметром `limit`, а не env-переменной —
как в реальном API. Замер только через `ab` (C-клиент), granian, 1 воркер, лучшее из
прогонов. Записи — одиночные; перед каждой операцией записи таблица восстанавливается
(TRUNCATE + 1000 строк), чтобы UPDATE/UPSERT/DELETE били по существующим строкам.

Запуск: .venv/bin/python bench/api_bench.py
Требует PostgreSQL на :5432 (таблицу `bench_rows` создаёт сам) и `ab` (apache2-utils).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import chart  # noqa: E402  (локальный модуль диаграмм рядом со стендом)

RESULTS_DIR = HERE / "results"
CHARTS_DIR = HERE.parent / "docs" / "img" / "charts"
VENV_PY = HERE.parent / ".venv" / "bin" / "python"
DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"

# limit как query-параметр (реальные значения пагинации); BENCH_LIMITS=1,2,5,10 — своя сетка
LIMITS = tuple(
    int(x) for x in os.environ.get("BENCH_LIMITS", "1,20,50,100,1000").split(",") if x.strip()
)
SERVER = os.environ.get("BENCH_SERVER", "granian")
WORKERS = int(os.environ.get("BENCH_WORKERS", "1"))
RUNS = int(os.environ.get("BENCH_RUNS", "3"))
# Кросс-языковые стенды в общей сессии (BENCH_NO_GO=1 / BENCH_NO_RUST=1 — отключить).
WITH_GO = os.environ.get("BENCH_NO_GO") != "1"
WITH_RUST = os.environ.get("BENCH_NO_RUST") != "1"
SELECT_REQUESTS = 5000
PING_REQUESTS = 10000
WRITE_REQUESTS = 3000

PORTS = {
    "ferronit": 8241,
    "ferronit-pipeline": 8246,
    "blacksheep": 8242,
    "litestar": 8243,
    "fastapi": 8244,
    "django": 8245,
    # Rust-стенд (bench/rust_http), тот же контракт и та же таблица
    "rust-axum": 8261,
    "rust-actix": 8262,
    # Go-стенд (bench/go_pg_orm), тот же контракт и та же таблица
    "go-pgx": 8251,
    "go-sqlx": 8252,
    "go-gorm": 8253,
    "go-ent": 8254,
}

GO_DIR = HERE / "go_pg_orm"
GO_BIN = GO_DIR / "go_pg_bench"
GO_MODES = {"go-pgx": "pgx", "go-sqlx": "sqlx", "go-gorm": "gorm", "go-ent": "ent"}

# pgbench — эталон «потолка БД»: тот же оператор, 50 клиентов, без слоя фреймворка.
# Запускаем из отдельного контейнера postgres:18 (на хосте нет postgresql-client).
PGBENCH_IMAGE = os.environ.get("BENCH_PGBENCH_IMAGE", "postgres:18")
WITH_PGBENCH = os.environ.get("BENCH_NO_PGBENCH") != "1"
PGBENCH_SECONDS = int(os.environ.get("BENCH_PGBENCH_SECONDS", "4"))
PGBENCH_CLIENTS = int(os.environ.get("BENCH_PGBENCH_CLIENTS", "50"))

# Тот же оператор, что шлёт приложение на этой стадии.
PGBENCH_SQL: dict[str, str] = {
    "rows1": "SELECT id, name, email, created_at, birthday, ttl FROM bench_rows LIMIT 1;",
    "rows20": "SELECT id, name, email, created_at, birthday, ttl FROM bench_rows LIMIT 20;",
    "rows50": "SELECT id, name, email, created_at, birthday, ttl FROM bench_rows LIMIT 50;",
    "rows100": "SELECT id, name, email, created_at, birthday, ttl FROM bench_rows LIMIT 100;",
    "rows1000": "SELECT id, name, email, created_at, birthday, ttl FROM bench_rows LIMIT 1000;",
    "insert": (
        "INSERT INTO bench_rows (name, email, created_at, birthday, ttl) "
        "VALUES ('bench', 'bench@example.com', '2026-01-15 12:00:00+00', "
        "'1990-01-15', '1 hour');"
    ),
    "update": (
        "UPDATE bench_rows SET name = 'bench', email = 'bench@example.com' WHERE id = 1;"
    ),
    "update-spread": (
        "UPDATE bench_rows SET name = 'bench', email = 'bench@example.com' "
        "WHERE id = 1 + (random() * 999)::int;"
    ),
    "upsert": (
        "INSERT INTO bench_rows (id, name, email, created_at, birthday, ttl) "
        "VALUES (1, 'bench', 'bench@example.com', '2026-01-15 12:00:00+00', "
        "'1990-01-15', '1 hour') "
        "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, email = EXCLUDED.email, "
        "created_at = EXCLUDED.created_at, birthday = EXCLUDED.birthday, ttl = EXCLUDED.ttl;"
    ),
    "upsert-spread": (
        "INSERT INTO bench_rows (id, name, email, created_at, birthday, ttl) "
        "SELECT 1 + (random() * 999)::int, 'bench', 'bench@example.com', "
        "'2026-01-15 12:00:00+00', '1990-01-15', '1 hour' "
        "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, email = EXCLUDED.email, "
        "created_at = EXCLUDED.created_at, birthday = EXCLUDED.birthday, ttl = EXCLUDED.ttl;"
    ),
    "delete": (
        "DELETE FROM bench_rows WHERE id = (SELECT id FROM bench_rows "
        "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED);"
    ),
}

RUST_DIR = HERE / "rust_http"
RUST_BINS = {"rust-axum": "axum_srv", "rust-actix": "actix_srv"}

# BENCH_APPS=ferronit,blacksheep — прогнать только выбранные варианты.
_BENCH_APPS = os.environ.get("BENCH_APPS")
if _BENCH_APPS:
    _wanted = {n.strip() for n in _BENCH_APPS.split(",") if n.strip()}
    PORTS = {k: v for k, v in PORTS.items() if k in _wanted}


FERRONIT_APP = '''\
import itertools
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

import msgspec

from ferronit import Body, Ferronit
from ferronit.contrib.rawdb import Pool, create_pool
from ferronit.contrib.rawmodel import Mapper, Model, Repository
from ferronit.ddd import AggregateRoot, Command, CommandBus, Query, QueryBus
from ferronit.di import Container
from ferronit.hexagonal import ApplicationService, Port

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
ShopPool = Annotated[Pool, "shop"]

# «Разброс» по строкам: ab всегда шлёт один и тот же body, поэтому id выбирает сервер.
SPREAD_ROWS = 1000
_spread = itertools.count()

# Даты/время для записи: фиксированные, одинаковые во всех фреймворках.
CREATED_AT = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
BIRTHDAY = date(1990, 1, 15)
TTL = timedelta(hours=1)


# ── Модель чтения: строка таблицы ─────────────────────────────────────

class BenchRow(Model):
    __table__ = "bench_rows"
    id: int = 0
    name: str = ""
    email: str = ""
    created_at: datetime = CREATED_AT
    birthday: date = BIRTHDAY
    ttl: timedelta = TTL


# ── Модели тела запроса ───────────────────────────────────────────────

class InsertBody(msgspec.Struct):
    name: str
    email: str


class UpsertBody(msgspec.Struct):
    id: int
    name: str
    email: str


# ── Агрегат: запись и чтение идут только через него ───────────────────

class Row(AggregateRoot):
    def __init__(self, row_id: int, name: str, email: str):
        super().__init__()
        self.id = row_id
        self.name = name
        self.email = email

    @classmethod
    def new(cls, name: str, email: str) -> "Row":
        return cls(0, name, email)

    def rename(self, name: str, email: str) -> None:
        self.name = name
        self.email = email


class RowMapper(Mapper[Row, BenchRow]):
    """Перевод «агрегат ↔ модель чтения» в обе стороны."""

    def to_aggregate(self, model: BenchRow) -> Row:
        return Row(model.id, model.name, model.email)

    def from_aggregate(self, aggregate: Row) -> BenchRow:
        return BenchRow(id=aggregate.id, name=aggregate.name, email=aggregate.email)


class RowsRepository(Repository[Row, BenchRow, RowMapper, ShopPool]):
    """Репозиторий агрегата Row на базе Repository фреймворка.

    Запись — агрегатом (insert/update/upsert/delete), чтение — базовым list(),
    который собирает агрегаты через RowMapper.to_aggregate; в ответ они
    проецируются обратно моделью чтения (to_read_model). INSERT без pk добавлен,
    потому что он идёт через базовый RawModelRepository юнита работы.
    """

    async def insert(self, name: str, email: str) -> Row:
        async with self.uow() as uow:
            saved = await uow.model(BenchRow).save(BenchRow(name=name, email=email))
        return self._mapper.to_aggregate(saved)

    async def update(self, row_id: int, name: str, email: str) -> Row | None:
        async with self.uow() as uow:
            saved = await uow.model(BenchRow).update(row_id, {"name": name, "email": email})
        return self._mapper.to_aggregate(saved) if saved is not None else None

    async def delete_oldest(self) -> None:
        """Удалить строку с минимальным id: id из URL повторять нельзя (ab бьёт один URL)."""
        async with self.uow() as uow:
            await uow.connection.execute(
                "DELETE FROM bench_rows WHERE id = (SELECT id FROM bench_rows "
                "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)"
            )


# ── CQRS: команды (запись) и запросы (чтение) ─────────────────────────

class InsertRow(Command):
    name: str
    email: str


class UpdateRow(Command):
    row_id: int
    name: str
    email: str


class UpsertRow(Command):
    row_id: int
    name: str
    email: str


class DeleteRow(Command):
    row_id: int


class DeleteOldest(Command):
    """Удалить самую «старую» строку таблицы — замер реального DELETE (не no-op)."""


class ListRows(Query):
    limit: int


class InsertRowHandler:
    def __init__(self, rows: RowsRepository):
        self._rows = rows

    async def __call__(self, cmd: InsertRow):
        return await self._rows.insert(cmd.name, cmd.email)


class UpdateRowHandler:
    def __init__(self, rows: RowsRepository):
        self._rows = rows

    async def __call__(self, cmd: UpdateRow):
        return await self._rows.update(cmd.row_id, cmd.name, cmd.email)


class UpsertRowHandler:
    def __init__(self, rows: RowsRepository):
        self._rows = rows

    async def __call__(self, cmd: UpsertRow):
        return await self._rows.save(Row(cmd.row_id, cmd.name, cmd.email))


class DeleteRowHandler:
    def __init__(self, rows: RowsRepository):
        self._rows = rows

    async def __call__(self, cmd: DeleteRow):
        await self._rows.delete(cmd.row_id)


class DeleteOldestHandler:
    def __init__(self, rows: RowsRepository):
        self._rows = rows

    async def __call__(self, cmd: DeleteOldest):
        await self._rows.delete_oldest()


class ListRowsHandler:
    def __init__(self, rows: RowsRepository):
        self._rows = rows

    async def __call__(self, query: ListRows) -> list[BenchRow]:
        return await self._rows.list_models(limit=query.limit)   # модели напрямую


# ── Входящий порт + прикладной сервис ─────────────────────────────────

class RowsApiPort(Port):
    """Входящий порт стенда — контракт операций над строками (в терминах домена)."""

    async def list(self, limit: int) -> list[BenchRow]: ...
    async def insert(self, name: str, email: str) -> Row: ...
    async def update(self, row_id: int, name: str, email: str) -> Row | None: ...
    async def delete(self, row_id: int) -> None: ...
    async def delete_oldest(self) -> None: ...
    async def upsert(self, row_id: int, name: str, email: str) -> Row: ...


class RowsService(ApplicationService, RowsApiPort):
    """Прикладной слой: реализует ``RowsApiPort``, диспатчит команды/запросы в шины."""

    def __init__(self, commands: CommandBus, queries: QueryBus):
        super().__init__()
        self._commands = commands
        self._queries = queries

    async def list(self, limit: int) -> list[BenchRow]:
        return await self._queries.ask(ListRows(limit))

    async def insert(self, name: str, email: str) -> Row:
        return await self._commands.dispatch(InsertRow(name, email))

    async def update(self, row_id: int, name: str, email: str) -> Row | None:
        return await self._commands.dispatch(UpdateRow(row_id, name, email))

    async def delete(self, row_id: int) -> None:
        await self._commands.dispatch(DeleteRow(row_id))

    async def delete_oldest(self) -> None:
        await self._commands.dispatch(DeleteOldest())

    async def upsert(self, row_id: int, name: str, email: str) -> Row:
        return await self._commands.dispatch(UpsertRow(row_id, name, email))


# ── DI: ключи (включая шины) регистрируются ДО объявления маршрутов ──

container = Container()
app = Ferronit(container=container)

COMMANDS = CommandBus()
QUERIES = QueryBus()
container.factory(RowsRepository)
container.singleton(CommandBus, COMMANDS)
container.singleton(QueryBus, QUERIES)
# Входящий порт → реализация (ДО объявления маршрутов — тот же порядок, что в примере).
container.factory(RowsApiPort, RowsService)


@app.lifespan
async def lifespan():
    pool = await create_pool(DSN, min_size=1, max_size=16)
    container.singleton(ShopPool, pool)
    rows = container.get(RowsRepository)
    COMMANDS.register(InsertRow, InsertRowHandler(rows))
    COMMANDS.register(UpdateRow, UpdateRowHandler(rows))
    COMMANDS.register(UpsertRow, UpsertRowHandler(rows))
    COMMANDS.register(DeleteRow, DeleteRowHandler(rows))
    COMMANDS.register(DeleteOldest, DeleteOldestHandler(rows))
    QUERIES.register(ListRows, ListRowsHandler(rows))
    yield
    await pool.close()


# ── HTTP-адаптер входящего порта ──────────────────────────────────────

class HttpRowsAdapter:
    """HTTP-адаптер: переводит HTTP-запросы в вызовы ``RowsApiPort``."""

    def mount(self, app: Ferronit, prefix: str = "") -> None:
        base = prefix.rstrip("/")

        @app.route(f"{base}/rows")
        async def rows(port: RowsApiPort, limit: int = 100):
            return {"rows": await port.list(limit)}

        @app.route(f"{base}/rows", methods=["POST"])
        async def insert(body: Annotated[InsertBody, Body()], port: RowsApiPort):
            row = await port.insert(body.name, body.email)
            return {"id": row.id}

        @app.route(f"{base}/rows/{{row_id}}", methods=["PUT"])
        async def update(
            row_id: int,
            body: Annotated[InsertBody, Body()],
            port: RowsApiPort,
        ):
            row = await port.update(row_id, body.name, body.email)
            return {"id": row.id if row is not None else None}

        @app.route(f"{base}/rows/any", methods=["PUT"])
        async def update_any(body: Annotated[InsertBody, Body()], port: RowsApiPort):
            row = await port.update(1 + next(_spread) % SPREAD_ROWS, body.name, body.email)
            return {"id": row.id if row is not None else None}

        @app.route(f"{base}/rows/upsert-any", methods=["POST"])
        async def upsert_any(body: Annotated[UpsertBody, Body()], port: RowsApiPort):
            row = await port.upsert(1 + next(_spread) % SPREAD_ROWS, body.name, body.email)
            return {"id": row.id}

        @app.route(f"{base}/rows/oldest", methods=["DELETE"])
        async def delete_oldest(port: RowsApiPort):
            await port.delete_oldest()
            return {"ok": True}

        @app.route(f"{base}/rows/{{row_id}}", methods=["DELETE"])
        async def delete(row_id: int, port: RowsApiPort):
            await port.delete(row_id)
            return {"ok": True}

        @app.route(f"{base}/rows/upsert", methods=["POST"])
        async def upsert(body: Annotated[UpsertBody, Body()], port: RowsApiPort):
            row = await port.upsert(body.id, body.name, body.email)
            return {"id": row.id}

        @app.route(f"{base}/ping")
        async def ping():
            return {"ok": True}


app.mount("", HttpRowsAdapter())
'''


# Второй вариант Ferronit: честный пайплайн движения данных списка —
# DBReadModel(BenchRow) → BenchRowAggregate(Row) → BenchRowOutApiModel(msgspec.Struct).
_PIPELINE_DTO = '''\
class BenchRowOutApiModel(msgspec.Struct):
    id: int
    name: str
    email: str
    created_at: datetime
    birthday: date
    ttl: timedelta
'''

_PIPELINE_HANDLER = '''\
class ListRowsHandler:
    def __init__(self, rows: RowsRepository):
        self._rows = rows
        self._mapper = RowMapper()

    async def __call__(self, query: ListRows) -> list[BenchRowOutApiModel]:
        models = await self._rows.list_models(limit=query.limit)   # БД → DBReadModel
        out = []
        for m in models:
            agg = self._mapper.to_aggregate(m)                     # → агрегат
            out.append(
                BenchRowOutApiModel(agg.id, agg.name, agg.email, m.created_at, m.birthday, m.ttl)
            )                                                      # → DTO
        return out
'''

_HANDLER_PLAIN = '''\
class ListRowsHandler:
    def __init__(self, rows: RowsRepository):
        self._rows = rows

    async def __call__(self, query: ListRows) -> list[BenchRow]:
        return await self._rows.list_models(limit=query.limit)   # модели напрямую
'''

FERRONIT_PIPELINE_APP = (
    FERRONIT_APP.replace(_HANDLER_PLAIN, _PIPELINE_DTO + "\n\n" + _PIPELINE_HANDLER)
    .replace("list[BenchRow]", "list[BenchRowOutApiModel]")
)


BLACKSHEEP_APP = '''\
import itertools
from datetime import UTC, date, datetime, timedelta

import msgspec
from blacksheep import Application, Request, Response
from blacksheep.contents import Content
from sqlalchemy import Column, Date, DateTime, Integer, Interval, MetaData, String, Table, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

DSN = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/postgres"

CREATED_AT = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
BIRTHDAY = date(1990, 1, 15)
TTL = timedelta(hours=1)

# «Разброс» по строкам: ab всегда шлёт один и тот же body, поэтому id выбирает сервер.
SPREAD_ROWS = 1000
_spread = itertools.count()

metadata = MetaData()
bench_rows = Table(
    "bench_rows", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String),
    Column("email", String),
    Column("created_at", DateTime(timezone=True)),
    Column("birthday", Date),
    Column("ttl", Interval),
)

engine = create_async_engine(DSN, pool_size=16)


def _json(data):
    return Response(200, content=Content(b"application/json", msgspec.json.encode(data)))


app = Application()


@app.router.get("/rows")
async def rows(request: Request):
    limit = int((request.query.get("limit") or [None])[0] or "100")
    async with engine.connect() as conn:
        result = await conn.execute(select(bench_rows).limit(limit))
        records = result.mappings().all()
    return _json({"rows": [dict(r) for r in records]})


@app.router.post("/rows")
async def insert(request: Request):
    data = await request.json()
    async with engine.begin() as conn:
        result = await conn.execute(
            bench_rows.insert()
            .values(
                name=data["name"], email=data["email"],
                created_at=CREATED_AT, birthday=BIRTHDAY, ttl=TTL,
            )
            .returning(bench_rows.c.id)
        )
        row_id = result.scalar_one()
    return _json({"id": row_id})


@app.router.put("/rows/{row_id}")
async def update(request: Request, row_id: str):
    data = await request.json()
    async with engine.begin() as conn:
        result = await conn.execute(
            bench_rows.update()
            .where(bench_rows.c.id == int(row_id))
            .values(name=data["name"], email=data["email"])
            .returning(bench_rows.c.id)
        )
        row_id_out = result.scalar_one_or_none()
    return _json({"id": row_id_out})


@app.router.delete("/rows/{row_id}")
async def delete(row_id: str):
    async with engine.begin() as conn:
        await conn.execute(bench_rows.delete().where(bench_rows.c.id == int(row_id)))
    return _json({"ok": True})


@app.router.put("/rows/any")
async def update_any(request: Request):
    data = await request.json()
    row_id = 1 + next(_spread) % SPREAD_ROWS
    async with engine.begin() as conn:
        result = await conn.execute(
            bench_rows.update().where(bench_rows.c.id == row_id)
            .values(name=data["name"], email=data["email"])
            .returning(bench_rows.c.id)
        )
        return _json({"id": result.scalar_one_or_none()})


@app.router.post("/rows/upsert-any")
async def upsert_any(request: Request):
    data = await request.json()
    row_id = 1 + next(_spread) % SPREAD_ROWS
    stmt = pg_insert(bench_rows).values(
        id=row_id, name=data["name"], email=data["email"],
        created_at=CREATED_AT, birthday=BIRTHDAY, ttl=TTL,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[bench_rows.c.id],
        set_={"name": stmt.excluded.name, "email": stmt.excluded.email,
              "created_at": stmt.excluded.created_at, "birthday": stmt.excluded.birthday,
              "ttl": stmt.excluded.ttl},
    ).returning(bench_rows.c.id)
    async with engine.begin() as conn:
        return _json({"id": (await conn.execute(stmt)).scalar_one()})


@app.router.delete("/rows/oldest")
async def delete_oldest():
    async with engine.begin() as conn:
        locked = (
            select(bench_rows.c.id)
            .order_by(bench_rows.c.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        await conn.execute(bench_rows.delete().where(bench_rows.c.id == locked))
    return _json({"ok": True})


@app.router.post("/rows/upsert")
async def upsert(request: Request):
    data = await request.json()
    stmt = pg_insert(bench_rows).values(
        id=data["id"], name=data["name"], email=data["email"],
        created_at=CREATED_AT, birthday=BIRTHDAY, ttl=TTL,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[bench_rows.c.id],
        set_={
            "name": stmt.excluded.name,
            "email": stmt.excluded.email,
            "created_at": stmt.excluded.created_at,
            "birthday": stmt.excluded.birthday,
            "ttl": stmt.excluded.ttl,
        },
    ).returning(bench_rows.c.id)
    async with engine.begin() as conn:
        result = await conn.execute(stmt)
        row_id = result.scalar_one()
    return _json({"id": row_id})


@app.router.get("/ping")
async def ping():
    return {"ok": True}
'''


LITESTAR_APP = '''\
import msgspec
import itertools
from datetime import UTC, date, datetime, timedelta

from litestar import Litestar, Request, delete as delete_op, get, post, put
from sqlalchemy import (
    Column, Date, DateTime, Integer, Interval, MetaData, String, Table, select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

DSN = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/postgres"

CREATED_AT = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
BIRTHDAY = date(1990, 1, 15)
TTL = timedelta(hours=1)

# «Разброс» по строкам: ab всегда шлёт один и тот же body, поэтому id выбирает сервер.
SPREAD_ROWS = 1000
_spread = itertools.count()

engine = create_async_engine(DSN, pool_size=16)

meta = MetaData()
bench_rows = Table(
    "bench_rows", meta,
    Column("id", Integer, primary_key=True),
    Column("name", String, nullable=False),
    Column("email", String, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("birthday", Date, nullable=False),
    Column("ttl", Interval, nullable=False),
)


def _row(row) -> dict:
    """Строка → dict в том же виде, что у остальных стендов."""
    return {
        "id": row.id, "name": row.name, "email": row.email,
        "created_at": row.created_at, "birthday": row.birthday, "ttl": row.ttl,
    }


@get("/rows")
async def rows(limit: int = 100) -> dict:
    async with engine.connect() as conn:
        result = await conn.execute(select(bench_rows).limit(limit))
        return {"rows": [_row(r) for r in result]}


@post("/rows")
async def insert(request: Request) -> dict:
    data = await request.json()
    async with engine.begin() as conn:
        result = await conn.execute(
            bench_rows.insert()
            .values(name=data["name"], email=data["email"],
                    created_at=CREATED_AT, birthday=BIRTHDAY, ttl=TTL)
            .returning(bench_rows.c.id)
        )
        return {"id": result.scalar_one()}


@put("/rows/{row_id:int}")
async def update(request: Request, row_id: int) -> dict:
    data = await request.json()
    async with engine.begin() as conn:
        result = await conn.execute(
            bench_rows.update()
            .where(bench_rows.c.id == row_id)
            .values(name=data["name"], email=data["email"])
            .returning(bench_rows.c.id)
        )
        return {"id": result.scalar_one_or_none()}


@post("/rows/upsert")
async def upsert(request: Request) -> dict:
    data = await request.json()
    stmt = pg_insert(bench_rows).values(
        id=data["id"], name=data["name"], email=data["email"],
        created_at=CREATED_AT, birthday=BIRTHDAY, ttl=TTL,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[bench_rows.c.id],
        set_={
            "name": stmt.excluded.name,
            "email": stmt.excluded.email,
            "created_at": stmt.excluded.created_at,
            "birthday": stmt.excluded.birthday,
            "ttl": stmt.excluded.ttl,
        },
    ).returning(bench_rows.c.id)
    async with engine.begin() as conn:
        return {"id": (await conn.execute(stmt)).scalar_one()}


@put("/rows/any")
async def update_any(request: Request) -> dict:
    data = await request.json()
    row_id = 1 + next(_spread) % SPREAD_ROWS
    async with engine.begin() as conn:
        result = await conn.execute(
            bench_rows.update().where(bench_rows.c.id == row_id)
            .values(name=data["name"], email=data["email"])
            .returning(bench_rows.c.id)
        )
        return {"id": result.scalar_one_or_none()}


@post("/rows/upsert-any")
async def upsert_any(request: Request) -> dict:
    data = await request.json()
    row_id = 1 + next(_spread) % SPREAD_ROWS
    stmt = pg_insert(bench_rows).values(
        id=row_id, name=data["name"], email=data["email"],
        created_at=CREATED_AT, birthday=BIRTHDAY, ttl=TTL,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[bench_rows.c.id],
        set_={"name": stmt.excluded.name, "email": stmt.excluded.email,
              "created_at": stmt.excluded.created_at, "birthday": stmt.excluded.birthday,
              "ttl": stmt.excluded.ttl},
    ).returning(bench_rows.c.id)
    async with engine.begin() as conn:
        return {"id": (await conn.execute(stmt)).scalar_one()}


@delete_op("/rows/oldest", status_code=200)
async def delete_oldest() -> dict:
    locked = (
        select(bench_rows.c.id)
        .order_by(bench_rows.c.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    async with engine.begin() as conn:
        await conn.execute(bench_rows.delete().where(bench_rows.c.id == locked))
    return {"ok": True}


@delete_op("/rows/{row_id:int}", status_code=200)
async def delete(row_id: int) -> dict:
    async with engine.begin() as conn:
        await conn.execute(bench_rows.delete().where(bench_rows.c.id == row_id))
    return {"ok": True}


@get("/ping")
async def ping() -> dict:
    return {"ok": True}


app = Litestar(route_handlers=[
    rows, insert, update, update_any, delete, delete_oldest, upsert, upsert_any, ping,
])
'''


FASTAPI_APP = '''\
import itertools
from datetime import UTC, date, datetime, timedelta

from fastapi import FastAPI, Query, Request
from sqlalchemy import (
    Column, Date, DateTime, Integer, Interval, String, select, text,
)
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

DSN = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/postgres"

CREATED_AT = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
BIRTHDAY = date(1990, 1, 15)
TTL = timedelta(hours=1)

# «Разброс» по строкам: ab всегда шлёт один и тот же body, поэтому id выбирает сервер.
SPREAD_ROWS = 1000
_spread = itertools.count()


class Base(DeclarativeBase):
    pass


class BenchRow(Base):
    __tablename__ = "bench_rows"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    birthday = Column(Date, nullable=False)
    ttl = Column(Interval, nullable=False)


engine = create_async_engine(DSN, echo=False)
factory = async_sessionmaker(engine, expire_on_commit=False)

app = FastAPI()


@app.get("/rows")
async def rows(limit: int = Query(100)):
    async with factory() as session:
        result = await session.execute(select(BenchRow).limit(limit))
        models = result.scalars().all()
    return {"rows": [
        {"id": r.id, "name": r.name, "email": r.email,
         "created_at": r.created_at, "birthday": r.birthday, "ttl": r.ttl}
        for r in models
    ]}


@app.post("/rows")
async def insert(request: Request):
    data = await request.json()
    async with factory() as session:
        row = BenchRow(name=data["name"], email=data["email"], created_at=CREATED_AT, birthday=BIRTHDAY, ttl=TTL)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return {"id": row.id}


@app.put("/rows/any")
async def update_any(request: Request):
    data = await request.json()
    row_id = 1 + next(_spread) % SPREAD_ROWS
    async with factory() as session:
        result = await session.execute(
            sa_update(BenchRow).where(BenchRow.id == row_id)
            .values(name=data["name"], email=data["email"]).returning(BenchRow.id)
        )
        await session.commit()
        return {"id": result.scalar_one_or_none()}


@app.post("/rows/upsert-any")
async def upsert_any(request: Request):
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    data = await request.json()
    row_id = 1 + next(_spread) % SPREAD_ROWS
    async with factory() as session:
        stmt = pg_insert(BenchRow).values(
            id=row_id, name=data["name"], email=data["email"],
            created_at=CREATED_AT, birthday=BIRTHDAY, ttl=TTL,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[BenchRow.id],
            set_={"name": stmt.excluded.name, "email": stmt.excluded.email,
                  "created_at": stmt.excluded.created_at, "birthday": stmt.excluded.birthday,
                  "ttl": stmt.excluded.ttl},
        ).returning(BenchRow.id)
        result = await session.execute(stmt)
        await session.commit()
        return {"id": result.scalar_one()}


@app.put("/rows/{row_id}")
async def update(row_id: int, request: Request):
    data = await request.json()
    async with factory() as session:
        # UPDATE через Core-выражение: ORM-сессия с изменением атрибутов не отправила бы
        # запрос, если значения совпали с прочитанными (dirty-tracking) — см. «грабли» в docs.
        result = await session.execute(
            sa_update(BenchRow)
            .where(BenchRow.id == row_id)
            .values(name=data["name"], email=data["email"])
            .returning(BenchRow.id)
        )
        await session.commit()
        return {"id": result.scalar_one_or_none()}


@app.delete("/rows/oldest")
async def delete_oldest():
    async with factory() as session:
        await session.execute(
            text(
                "DELETE FROM bench_rows WHERE id = (SELECT id FROM bench_rows "
                "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)"
            )
        )
        await session.commit()
    return {"ok": True}


@app.delete("/rows/{row_id}")
async def delete(row_id: int):
    async with factory() as session:
        result = await session.execute(select(BenchRow).where(BenchRow.id == row_id))
        row = result.scalars().first()
        if row is not None:
            await session.delete(row)
            await session.commit()
    return {"ok": True}




@app.post("/rows/upsert")
async def upsert(request: Request):
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    data = await request.json()
    async with factory() as session:
        stmt = pg_insert(BenchRow).values(
            id=data["id"], name=data["name"], email=data["email"],
            created_at=CREATED_AT, birthday=BIRTHDAY, ttl=TTL,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[BenchRow.id],
            set_={
                "name": stmt.excluded.name,
                "email": stmt.excluded.email,
                "created_at": stmt.excluded.created_at,
                "birthday": stmt.excluded.birthday,
                "ttl": stmt.excluded.ttl,
            },
        ).returning(BenchRow.id)
        result = await session.execute(stmt)
        await session.commit()
        row_id = result.scalars().first()
        return {"id": row_id}


@app.get("/ping")
async def ping():
    return {"ok": True}
'''


APPS = {
    "ferronit": FERRONIT_APP,
    "ferronit-pipeline": FERRONIT_PIPELINE_APP,
    "blacksheep": BLACKSHEEP_APP,
    "litestar": LITESTAR_APP,
    "fastapi": FASTAPI_APP,
}

# Тела одиночных записей (INSERT / UPDATE / UPSERT).
INSERT_BODY = '{"name":"bench","email":"bench@example.com"}'
UPSERT_BODY = '{"id":1,"name":"bench","email":"bench@example.com"}'


def seed(rows: int = 1000) -> None:
    """Готовит таблицу ``bench_rows`` ровно с ``rows`` строками (id 1..rows).

    Args:
        rows: Сколько строк оставить в таблице.
    """
    import asyncpg

    async def _seed() -> None:
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS bench_rows ("
                "id serial PRIMARY KEY, name text NOT NULL, email text NOT NULL, "
                "created_at timestamptz NOT NULL, birthday date NOT NULL, ttl interval NOT NULL)"
            )
            await conn.execute("TRUNCATE bench_rows RESTART IDENTITY")
            await conn.execute(
                "INSERT INTO bench_rows (name, email, created_at, birthday, ttl) "
                "SELECT 'user_' || g, 'u' || g || '@example.com', "
                "TIMESTAMPTZ '2026-01-01 00:00:00+00' + (g || ' hours')::interval, "
                "DATE '1990-01-01' + g, "
                "(g || ' minutes')::interval "
                f"FROM generate_series(1, {rows}) AS g"
            )
        finally:
            await conn.close()

    asyncio.run(_seed())


def ensure_go() -> bool:
    """Собирает Go-стенд, если бинарника нет или он старше исходников.

    Returns:
        bool: True — Go-стенд доступен и его приложения можно мерить в этой сессии.
    """
    src = GO_DIR / "main.go"
    if not src.exists():
        return False
    fresh = GO_BIN.exists() and GO_BIN.stat().st_mtime >= src.stat().st_mtime
    if fresh:
        return True
    try:
        proc = subprocess.run(
            ["go", "build", "-o", str(GO_BIN), "."],
            cwd=GO_DIR, capture_output=True, text=True, timeout=600,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"! Go-стенд не собран ({exc}) — меряю только Python-фреймворки")
        return False
    if proc.returncode != 0:
        print(f"! go build упал — меряю только Python-фреймворки:\n{proc.stderr[-600:]}")
        return False
    return True


def ensure_rust() -> bool:
    """Собирает Rust-стенд, если бинарников нет или они старше исходников.

    Returns:
        bool: True — Axum/Actix доступны и их можно мерить в этой сессии.
    """
    newest_src = max(
        (p.stat().st_mtime for p in (RUST_DIR / "src").rglob("*.rs")),
        default=0.0,
    )
    cargo = RUST_DIR / "Cargo.toml"
    if cargo.exists():
        newest_src = max(newest_src, cargo.stat().st_mtime)
    bins = [RUST_DIR / "target" / "release" / b for b in RUST_BINS.values()]
    if all(b.exists() and b.stat().st_mtime >= newest_src for b in bins):
        return True
    try:
        proc = subprocess.run(
            ["cargo", "build", "--release"],
            cwd=RUST_DIR, capture_output=True, text=True, timeout=1800,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"! Rust-стенд не собран ({exc}) — меряю без Axum/Actix")
        return False
    if proc.returncode != 0:
        print(f"! cargo build упал — меряю без Axum/Actix:\n{proc.stderr[-600:]}")
        return False
    return all(b.exists() for b in bins)


def start(name: str, port: int, workers: int = WORKERS) -> subprocess.Popen:
    """Пишет приложение и поднимает его на выбранном сервере."""
    if name in RUST_BINS:
        binary = RUST_DIR / "target" / "release" / RUST_BINS[name]
        # axum: число потоков tokio из env; actix: --workers
        env = {**os.environ, "TOKIO_WORKER_THREADS": str(workers)}
        return subprocess.Popen(
            [str(binary), "--port", str(port), "--pool", str(max(16, workers)),
             "--workers", str(workers)],
            cwd=RUST_DIR, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    if name in GO_MODES:
        env = {**os.environ, "GOMAXPROCS": str(workers)}  # аналог «1 воркер» у granian
        return subprocess.Popen(
            [str(GO_BIN), "--serve", "--mode", GO_MODES[name], "--port", str(port),
             "--pool", str(max(16, workers))],
            cwd=GO_DIR, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    if name == "django":
        cmd = [
            str(VENV_PY), "-m", SERVER, "--interface", "asgi",
            "--host", "127.0.0.1", "--port", str(port), "--workers", str(workers),
            "--loop", "uvloop", "--no-ws", "--log-level", "warning",
            "django_bench.asgi:application",
        ]
    else:
        module = name.replace("-", "_")
        (HERE / f"_bench_api_{module}.py").write_text(APPS[name])
        rsgi = name.startswith("ferronit")  # все варианты Ferronit — на RSGI (основной стек)
        iface = "rsgi" if rsgi else "asgi"
        cmd = [
            str(VENV_PY), "-m", SERVER, "--interface", iface,
            "--host", "127.0.0.1", "--port", str(port), "--workers", str(workers),
            "--loop", "uvloop",
        ]
        if not rsgi:
            cmd.append("--no-ws")
        cmd += ["--log-level", "warning", f"_bench_api_{module}:app"]
    return subprocess.Popen(
        cmd,
        cwd=HERE,
        env={**os.environ, "DJANGO_SETTINGS_MODULE": "django_bench.settings"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _children(pid: int) -> list[int]:
    """Прямые потомки процесса (без psutil, через /proc)."""
    try:
        with open(f"/proc/{pid}/task/{pid}/children") as f:
            return [int(x) for x in f.read().split()]
    except (FileNotFoundError, ProcessLookupError):
        return []


def rss_kb(pid: int) -> int:
    """Суммарный RSS процесса и всех его потомков (granian supervisor + worker), в КБ."""
    total = 0
    stack = [pid]
    seen: set[int] = set()
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        try:
            with open(f"/proc/{p}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        total += int(line.split()[1])
                        break
        except (FileNotFoundError, ProcessLookupError):
            pass
        stack.extend(_children(p))
    return total


def pss_kb(pid: int) -> int:
    """Суммарный PSS процесса и всех его потомков, в КБ.

    PSS (proportional set size) делит каждую общую страницу между процессами,
    которые её отобразили, поэтому сумма по дереву процессов даёт **реальную**
    физическую память сервера — в отличие от RSS, который считает общие страницы
    (libpython, `.so` драйверов, бинарь сервера) в каждом процессе заново.

    Args:
        pid: PID корня (supervisor granian или сам процесс сервера).

    Returns:
        Физическая память дерева процессов в КБ.
    """
    total = 0
    stack = [pid]
    seen: set[int] = set()
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        try:
            with open(f"/proc/{p}/smaps_rollup") as f:
                for line in f:
                    if line.startswith("Pss:"):
                        total += int(line.split()[1])
                        break
        except (FileNotFoundError, ProcessLookupError):
            pass
        stack.extend(_children(p))
    return total


def wait_ready(port: int, proc: subprocess.Popen | None = None, timeout: float = 20.0) -> None:
    """Ждёт готовности сервера; если ``proc`` умер — падает (порт занят старым процессом)."""
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(
                f"процесс на :{port} завершился с кодом {proc.returncode} — порт занят?"
            )
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise TimeoutError(f"сервер на :{port} не поднялся")


def ab_reqs(port: int, path: str, n: int) -> float:
    out = subprocess.run(
        ["ab", "-n", str(n), "-c", "50", "-k", f"http://127.0.0.1:{port}{path}"],
        capture_output=True, text=True, timeout=300,
    ).stdout
    match = re.search(r"Requests per second:\s+([\d.]+)", out)
    if not match:
        raise RuntimeError(f"ab не дал результат для {path}:\n{out[-400:]}")
    return float(match.group(1))


def ab_write(port: int, path: str, method: str, body: str | None, n: int) -> float:
    """req/s по ab для одиночной записи: POST (-p), PUT (-u), DELETE (-m DELETE)."""
    body_file = HERE / "_bench_write.json"
    cmd = ["ab", "-n", str(n), "-c", "50", "-k"]
    if method == "POST":
        body_file.write_text(body or "")
        cmd += ["-T", "application/json", "-p", str(body_file)]
    elif method == "PUT":
        body_file.write_text(body or "")
        cmd += ["-T", "application/json", "-u", str(body_file)]
    elif method == "DELETE":
        cmd += ["-m", "DELETE"]
    cmd.append(f"http://127.0.0.1:{port}{path}")
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300).stdout
    match = re.search(r"Requests per second:\s+([\d.]+)", out)
    if not match:
        raise RuntimeError(f"ab не дал результат для {method} {path}:\n{out[-400:]}")
    return float(match.group(1))


def measure_round_robin(
    measure, reseed: bool = False, seed_rows: int = 1000
) -> dict[str, float]:
    """Прогнать ``measure(name, port)`` по всем приложениям, ``RUNS`` раундов по кругу.

    Раунд — «каждое приложение по одному разу»: медленный дрейф машины (прогрев
    CPU, шум соседей) сдвигает все замеры раунда одинаково, поэтому отношения
    между приложениями сходятся между раундами, а внутри раунда — честные.

    Args:
        measure: функция ``(name, port) -> float``, возвращающая req/s.
        reseed: восстанавливать таблицу перед замером (для записей).

    Returns:
        Лучший результат по каждому приложению за все раунды.
    """
    best = {name: 0.0 for name in PORTS}
    for _ in range(RUNS):
        for name, port in PORTS.items():
            proc = start(name, port)
            try:
                wait_ready(port, proc)
                if reseed:
                    seed(seed_rows)
                best[name] = max(best[name], measure(name, port))
            finally:
                proc.terminate()
                proc.wait()
    return best


def _transport_note() -> str:
    """Строка о транспортах: Ferronit — RSGI, Python-остальные — ASGI, Go — net/http."""
    parts = [f"Ferronit/RSGI ({SERVER})"]
    if any(not n.startswith("go-") and not n.startswith("ferronit") for n in PORTS):
        parts.append(f"Python/ASGI ({SERVER})")
    if any(n.startswith("rust-") for n in PORTS):
        parts.append("Rust (axum/actix, 1 поток)")
    if any(n.startswith("go-") for n in PORTS):
        parts.append("Go/net/http (GOMAXPROCS=1)")
    return ", ".join(parts)


def stage_ping() -> dict[str, float]:
    """Стадия `ping`: пустой маршрут без БД."""
    return measure_round_robin(lambda _n, port: ab_reqs(port, "/ping", PING_REQUESTS))


def stage_rows(limit: int) -> dict[str, float]:
    """Стадия `rows<limit>`: чтение N строк с query-параметром."""
    return measure_round_robin(
        lambda _n, port: ab_reqs(port, f"/rows?limit={limit}", SELECT_REQUESTS)
    )


def stage_write(method: str, path: str, body: str | None, seed_rows: int) -> dict[str, float]:
    """Стадия записи: перед каждым замером таблица восстанавливается."""
    return measure_round_robin(
        lambda _n, port: ab_write(port, path, method, body, WRITE_REQUESTS),
        reseed=True,
        seed_rows=seed_rows,
    )


def pgbench_tps(stage: str) -> float | None:
    """Эталон ``pgbench`` для стадии: тот же оператор, 50 клиентов, без фреймворка.

    Клиент живёт в отдельном контейнере ``postgres:18`` (на хосте нет postgresql-client),
    подключается к тому же серверу через ``--network host``. Это «потолок БД»: сколько
    транзакций в секунду выдерживает PostgreSQL на том же операторе и той же конкурентности.

    Args:
        stage: Имя стадии; для стадий без БД (``ping``, ``mem*``) возвращается ``None``.

    Returns:
        Транзакций в секунду по данным ``pgbench`` или ``None``, если эталон не применим.
    """
    sql = PGBENCH_SQL.get(stage)
    if not sql or not WITH_PGBENCH:
        return None
    if stage == "delete":  # каждому запросу нужна живая строка
        seed(WRITE_REQUESTS + 1000)
    script_dir = HERE / "pgbench_scripts"
    script_dir.mkdir(exist_ok=True)
    script = script_dir / f"{stage}.sql"
    script.write_text(sql + "\n")
    cmd = [
        "docker", "run", "--rm", "--network", "host",
        "-e", "PGPASSWORD=postgres",
        "-v", f"{script_dir}:/scripts:ro",
        PGBENCH_IMAGE, "pgbench",
        "-h", "127.0.0.1", "-p", "5432", "-U", "postgres", "-d", "postgres",
        "-n", "-c", str(PGBENCH_CLIENTS), "-j", "4",
        "-T", str(PGBENCH_SECONDS), "-f", f"/scripts/{stage}.sql",
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=600).stdout
    match = re.search(r"tps = ([\d.]+)", out)
    if not match:
        print(f"! pgbench не дал tps для стадии {stage}:\n{out[-400:]}")
        return None
    return float(match.group(1))


def stage_memory(workers: int) -> dict[str, float]:
    """Стадия памяти: PSS серверного процесса (дерева процессов) после прогрева, в МБ.

    Считаем PSS, а не RSS: RSS суммирует общие страницы в каждом процессе дерева
    (supervisor + helper + worker у granian), из-за чего «память на воркер» выглядит
    вдвое больше реальной.
    """
    values: dict[str, float] = {}
    for name, port in PORTS.items():
        proc = start(name, port, workers=workers)
        try:
            wait_ready(port, proc)
            ab_reqs(port, "/ping", PING_REQUESTS)
            ab_reqs(port, "/rows?limit=1000", SELECT_REQUESTS)
            values[name] = pss_kb(proc.pid) / 1024.0
        finally:
            proc.terminate()
            proc.wait()
    return values


# Стадия → функция замера. Имена совпадают с именами файлов диаграмм.
STAGES: dict[str, Callable[[], dict[str, float]]] = {"ping": stage_ping}
for _limit in LIMITS:
    STAGES[f"rows{_limit}"] = lambda lim=_limit: stage_rows(lim)
STAGES["insert"] = lambda: stage_write("POST", "/rows", INSERT_BODY, 1000)
STAGES["update"] = lambda: stage_write("PUT", "/rows/1", INSERT_BODY, 1000)
STAGES["upsert"] = lambda: stage_write("POST", "/rows/upsert", UPSERT_BODY, 1000)
# Те же операции, но id выбирает сервер: 50 клиентов бьют в разные строки,
# поэтому видно, сколько стоит блокировка «горячей» строки, а сколько — fsync.
STAGES["update-spread"] = lambda: stage_write("PUT", "/rows/any", INSERT_BODY, 1000)
STAGES["upsert-spread"] = lambda: stage_write("POST", "/rows/upsert-any", UPSERT_BODY, 1000)
# DELETE должен удалять живую строку: сеем больше строк, чем запросов.
STAGES["delete"] = lambda: stage_write("DELETE", "/rows/oldest", None, WRITE_REQUESTS + 1000)
if os.environ.get("BENCH_MEMORY") == "1":
    STAGES["mem1"] = lambda: stage_memory(1)
    STAGES["mem10"] = lambda: stage_memory(10)


def _stage_file(stage: str) -> Path:
    """Путь к JSON с результатами стадии."""
    return RESULTS_DIR / f"stage-{stage}.json"


def run_stage(stage: str) -> dict[str, float]:
    """Прогнать одну стадию, напечатать таблицу, сохранить JSON и диаграмму.

    Args:
        stage: Имя стадии из :data:`STAGES`.

    Returns:
        Результаты по приложениям (req/s или МБ для стадий памяти).
    """
    values = STAGES[stage]()
    tps = pgbench_tps(stage)
    # Базовая линия — ferronit-pipeline; если его не мерили (BENCH_APPS), берём лучший результат.
    has_baseline = chart.BASELINE in values
    base = values[chart.BASELINE] if has_baseline else max(values.values())
    unit = "МБ PSS" if stage.startswith("mem") else "req/s"
    note = (
        ""
        if has_baseline
        else f"  ({chart.BASELINE_LABEL} не мерился: базовая линия — лучший результат)"
    )
    print(f"\n### {stage} — {chart.stage_title(stage)} ({unit}, раундов {RUNS}){note}")
    for name, value in sorted(values.items(), key=lambda kv: -kv[1]):
        ratio = value / base
        mark = "  ← baseline" if name == chart.BASELINE else ""
        print(f"  {name:18s} {value:>9,.0f} {unit:6s} ×{ratio:.2f}{mark}")

    record: dict[str, object] = {
        "stage": stage,
        "values": values,
        "runs": RUNS,
        "requests": {"ping": PING_REQUESTS, "rows": SELECT_REQUESTS, "write": WRITE_REQUESTS},
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    reference = None
    if tps is not None:
        record["pgbench_tps"] = tps
        reference = ("pgbench (DB ceiling)", tps, tps / base)
        print(f"  pgbench (потолок БД) {tps:>9,.0f} ×{tps / base:.1f}".replace(",", " "))
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    _stage_file(stage).write_text(
        json.dumps(record, indent=2, ensure_ascii=False)
    )
    svg = chart.render_stage(
        stage, values, CHARTS_DIR, subtitle=f"best of {RUNS} rounds", unit=unit,
        reference=reference,
    )
    png = svg.with_suffix(".png")
    print(f"  диаграмма: {png if png.exists() else svg}")
    return values


def selected_stages() -> list[str]:
    """Стадии из ``BENCH_STAGES`` (по умолчанию — все)."""
    raw = os.environ.get("BENCH_STAGES", "all").strip()
    if raw in ("", "all"):
        return list(STAGES)
    wanted = [w.strip() for w in raw.split(",") if w.strip()]
    unknown = [w for w in wanted if w not in STAGES]
    if unknown:
        raise SystemExit(f"неизвестные стадии: {unknown}; есть: {list(STAGES)}")
    return wanted


def main() -> None:
    global PORTS
    if WITH_GO and ensure_go():
        pass
    else:
        if WITH_GO:
            print("! Go-стенд недоступен — меряю без него")
        PORTS = {k: v for k, v in PORTS.items() if k not in GO_MODES}
    if WITH_RUST and ensure_rust():
        pass
    else:
        if WITH_RUST:
            print("! Rust-стенд недоступен — меряю без Axum/Actix")
        PORTS = {k: v for k, v in PORTS.items() if k not in RUST_BINS}

    seed()
    stages = selected_stages()
    print(f"{_transport_note()}, ab -c 50 -k, раундов {RUNS}, приложений {len(PORTS)}")
    print(f"стадии: {', '.join(stages)}")

    results: dict[str, dict[str, float]] = {}
    for stage in stages:
        results[stage] = run_stage(stage)

    speed = {s: v for s, v in results.items() if not s.startswith("mem")}
    if len(speed) > 1 and chart.BASELINE in PORTS:
        svg = chart.render_combined(
            speed, CHARTS_DIR, subtitle=f"same session, best of {RUNS} rounds"
        )
        print(f"\nсводная диаграмма: {svg.with_suffix('.png')}")


if __name__ == "__main__":
    main()
