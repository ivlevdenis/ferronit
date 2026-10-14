"""Реалистичный API-бенч: ping + строки из БД с query-параметром + одиночные записи.

Пять фреймворков (Ferronit, BlackSheep, Litestar, FastAPI, Django), одинаковые
эндпоинты, каждый — своим идиоматичным путём (Ferronit=rawmodel, BlackSheep/Litestar=
asyncpg+msgspec, FastAPI=SQLAlchemy ORM, Django=Django ORM).

Эндпоинты:
    GET  /ping              -> {"ok": true}
    GET  /rows?limit=N      -> N строк SELECT id, name, email LIMIT $1 (N из query)
    POST /rows              -> INSERT одной строки {name, email}
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
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV_PY = HERE.parent / ".venv" / "bin" / "python"
DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"

# limit как query-параметр (реальные значения пагинации)
LIMITS = (1, 20, 50, 100, 1000)
SERVER = os.environ.get("BENCH_SERVER", "granian")
RUNS = 2
SELECT_REQUESTS = 5000
PING_REQUESTS = 10000
WRITE_REQUESTS = 3000

PORTS = {
    "ferronit": 8241,
    "blacksheep": 8242,
    "litestar": 8243,
    "fastapi": 8244,
    "django": 8245,
}

# BENCH_APPS=ferronit,blacksheep — прогнать только выбранные варианты.
_BENCH_APPS = os.environ.get("BENCH_APPS")
if _BENCH_APPS:
    _wanted = {n.strip() for n in _BENCH_APPS.split(",") if n.strip()}
    PORTS = {k: v for k, v in PORTS.items() if k in _wanted}


FERRONIT_APP = '''\
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


# ── Модель чтения: строка таблицы ─────────────────────────────────────

class BenchRow(Model):
    __table__ = "bench_rows"
    id: int = 0
    name: str = ""
    email: str = ""


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

    def to_read_model(self, aggregate: Row) -> BenchRow:
        """Проекция агрегата в модель чтения — то, что уходит в ответ."""
        return self._mapper.from_aggregate(aggregate)


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


class ListRowsHandler:
    def __init__(self, rows: RowsRepository):
        self._rows = rows

    async def __call__(self, query: ListRows) -> list[BenchRow]:
        aggregates = await self._rows.list(limit=query.limit)   # агрегаты собираются
        return [self._rows.to_read_model(a) for a in aggregates]


# ── Входящий порт + прикладной сервис ─────────────────────────────────

class RowsApiPort(Port):
    """Входящий порт стенда — контракт операций над строками (в терминах домена)."""

    async def list(self, limit: int) -> list[BenchRow]: ...
    async def insert(self, name: str, email: str) -> Row: ...
    async def update(self, row_id: int, name: str, email: str) -> Row | None: ...
    async def delete(self, row_id: int) -> None: ...
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


BLACKSHEEP_APP = '''\
import asyncio

import asyncpg
import msgspec
from blacksheep import Application, Request, Response
from blacksheep.contents import Content

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"

_pool = None
_lock = asyncio.Lock()


async def get_pool():
    global _pool
    if _pool is None:
        async with _lock:
            if _pool is None:
                _pool = await asyncpg.create_pool(DSN, min_size=1, max_size=16)
    return _pool


def _json(data):
    return Response(200, content=Content(b"application/json", msgspec.json.encode(data)))


app = Application()


@app.router.get("/rows")
async def rows(request: Request):
    limit = int((request.query.get("limit") or [None])[0] or "100")
    pool = await get_pool()
    async with pool.acquire() as conn:
        records = await conn.fetch("SELECT id, name, email FROM bench_rows LIMIT $1", limit)
    return _json({"rows": [dict(r) for r in records]})


@app.router.post("/rows")
async def insert(request: Request):
    data = await request.json()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO bench_rows (name, email) VALUES ($1, $2) RETURNING id",
            data["name"], data["email"],
        )
    return _json({"id": row["id"]})


@app.router.put("/rows/{row_id}")
async def update(request: Request, row_id: str):
    data = await request.json()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE bench_rows SET name = $1, email = $2 WHERE id = $3 RETURNING id",
            data["name"], data["email"], int(row_id),
        )
    return _json({"id": row["id"] if row is not None else None})


@app.router.delete("/rows/{row_id}")
async def delete(row_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM bench_rows WHERE id = $1", int(row_id))
    return _json({"ok": True})


@app.router.post("/rows/upsert")
async def upsert(request: Request):
    data = await request.json()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO bench_rows (id, name, email) VALUES ($1, $2, $3) "
            "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, email = EXCLUDED.email "
            "RETURNING id",
            data["id"], data["name"], data["email"],
        )
    return _json({"id": row["id"]})


@app.router.get("/ping")
async def ping():
    return {"ok": True}
'''


LITESTAR_APP = '''\
import asyncio

import asyncpg
import msgspec
from litestar import Litestar, Request, get, post, put, delete

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"

_pool = None
_lock = asyncio.Lock()


async def get_pool():
    global _pool
    if _pool is None:
        async with _lock:
            if _pool is None:
                _pool = await asyncpg.create_pool(DSN, min_size=1, max_size=16)
    return _pool


@get("/rows")
async def rows(limit: int = 100) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        records = await conn.fetch("SELECT id, name, email FROM bench_rows LIMIT $1", limit)
    return {"rows": [dict(r) for r in records]}


@post("/rows")
async def insert(request: Request) -> dict:
    data = await request.json()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO bench_rows (name, email) VALUES ($1, $2) RETURNING id",
            data["name"], data["email"],
        )
    return {"id": row["id"]}


@put("/rows/{row_id:int}")
async def update(request: Request, row_id: int) -> dict:
    data = await request.json()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE bench_rows SET name = $1, email = $2 WHERE id = $3 RETURNING id",
            data["name"], data["email"], row_id,
        )
    return {"id": row["id"] if row is not None else None}


@delete("/rows/{row_id:int}", status_code=200)
async def delete(row_id: int) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM bench_rows WHERE id = $1", row_id)
    return {"ok": True}


@post("/rows/upsert")
async def upsert(request: Request) -> dict:
    data = await request.json()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO bench_rows (id, name, email) VALUES ($1, $2, $3) "
            "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, email = EXCLUDED.email "
            "RETURNING id",
            data["id"], data["name"], data["email"],
        )
    return {"id": row["id"]}


@get("/ping")
async def ping() -> dict:
    return {"ok": True}


app = Litestar(route_handlers=[rows, insert, update, delete, upsert, ping])
'''


FASTAPI_APP = '''\
from fastapi import FastAPI, Query, Request
from sqlalchemy import Column, Integer, String, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

DSN = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/postgres"


class Base(DeclarativeBase):
    pass


class BenchRow(Base):
    __tablename__ = "bench_rows"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False)


engine = create_async_engine(DSN, echo=False)
factory = async_sessionmaker(engine, expire_on_commit=False)

app = FastAPI()


@app.get("/rows")
async def rows(limit: int = Query(100)):
    async with factory() as session:
        result = await session.execute(select(BenchRow).limit(limit))
        models = result.scalars().all()
    return {"rows": [{"id": r.id, "name": r.name, "email": r.email} for r in models]}


@app.post("/rows")
async def insert(request: Request):
    data = await request.json()
    async with factory() as session:
        row = BenchRow(name=data["name"], email=data["email"])
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return {"id": row.id}


@app.put("/rows/{row_id}")
async def update(row_id: int, request: Request):
    data = await request.json()
    async with factory() as session:
        result = await session.execute(select(BenchRow).where(BenchRow.id == row_id))
        row = result.scalars().first()
        if row is None:
            return {"id": None}
        row.name = data["name"]
        row.email = data["email"]
        await session.commit()
        return {"id": row.id}


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
        stmt = pg_insert(BenchRow).values(id=data["id"], name=data["name"], email=data["email"])
        stmt = stmt.on_conflict_do_update(
            index_elements=[BenchRow.id],
            set_={"name": stmt.excluded.name, "email": stmt.excluded.email},
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
    "blacksheep": BLACKSHEEP_APP,
    "litestar": LITESTAR_APP,
    "fastapi": FASTAPI_APP,
}

# Тела одиночных записей (INSERT / UPDATE / UPSERT).
INSERT_BODY = '{"name":"bench","email":"bench@example.com"}'
UPSERT_BODY = '{"id":1,"name":"bench","email":"bench@example.com"}'


def seed() -> None:
    """Готовит таблицу ``bench_rows`` ровно с 1000 строками (id 1..1000)."""
    import asyncpg

    async def _seed() -> None:
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS bench_rows ("
                "id serial PRIMARY KEY, name text NOT NULL, email text NOT NULL)"
            )
            await conn.execute("TRUNCATE bench_rows RESTART IDENTITY")
            await conn.execute(
                "INSERT INTO bench_rows (name, email) "
                "SELECT 'user_' || g, 'u' || g || '@example.com' "
                "FROM generate_series(1, 1000) AS g"
            )
        finally:
            await conn.close()

    asyncio.run(_seed())


def start(name: str, port: int) -> subprocess.Popen:
    """Пишет приложение и поднимает его одним воркером на выбранном сервере."""
    if name == "django":
        cmd = [
            str(VENV_PY), "-m", SERVER, "--interface", "asgi",
            "--host", "127.0.0.1", "--port", str(port), "--workers", "1",
            "--no-ws", "--log-level", "warning", "django_bench.asgi:application",
        ]
    else:
        module = name.replace("-", "_")
        (HERE / f"_bench_api_{module}.py").write_text(APPS[name])
        cmd = [
            str(VENV_PY), "-m", SERVER, "--interface", "asgi",
            "--host", "127.0.0.1", "--port", str(port), "--workers", "1",
            "--no-ws", "--log-level", "warning", f"_bench_api_{module}:app",
        ]
    return subprocess.Popen(
        cmd,
        cwd=HERE,
        env={**os.environ, "DJANGO_SETTINGS_MODULE": "django_bench.settings"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_ready(port: int, timeout: float = 20.0) -> None:
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
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


def best_of(port: int, measure) -> float:
    """Лучшее из ``RUNS`` прогонов."""
    best = 0.0
    for _ in range(RUNS):
        best = max(best, measure(port))
    return best


def run_select() -> None:
    """GET /ping и GET /rows?limit=N (1/20/50/100/1000), по очереди."""
    names = list(PORTS)
    print(f"сервер {SERVER}, 1 воркер, ab -c 50 -k, лучшее из {RUNS}\n")

    print("GET /ping (простой dict), req/s:")
    pings = {}
    for name, port in PORTS.items():
        proc = start(name, port)
        try:
            wait_ready(port)
            pings[name] = best_of(port, lambda p: ab_reqs(p, "/ping", PING_REQUESTS))
        finally:
            proc.terminate()
            proc.wait()
    for name in names:
        print(f"  {name:12s} {pings[name]:>12,.0f}")

    print("\nGET /rows?limit=N (SELECT с query-параметром), req/s:")
    header = f"{'limit':>5} | " + " | ".join(f"{name:>12}" for name in names)
    print(header)
    print("-" * len(header))
    for limit in LIMITS:
        cells = {}
        for name, port in PORTS.items():
            proc = start(name, port)
            try:
                wait_ready(port)
                cells[name] = best_of(
                    port, lambda p: ab_reqs(p, f"/rows?limit={limit}", SELECT_REQUESTS)
                )
            finally:
                proc.terminate()
                proc.wait()
        row = " | ".join(f"{cells[name]:>12,.0f}" for name in names)
        print(f"{limit:>5} | {row}")


def run_writes() -> None:
    """Одиночные записи: INSERT / UPDATE / UPSERT / DELETE. Перед каждой — re-seed."""
    ops = [
        ("INSERT", "POST", "/rows", INSERT_BODY),
        ("UPDATE", "PUT", "/rows/1", INSERT_BODY),
        ("UPSERT", "POST", "/rows/upsert", UPSERT_BODY),
        ("DELETE", "DELETE", "/rows/1", None),
    ]
    names = list(PORTS)
    print("\nОдиночные записи (ab -c 50 -k, req/s):")
    header = f"{'операция':>8} | " + " | ".join(f"{name:>12}" for name in names)
    print(header)
    print("-" * len(header))
    for op, method, path, body in ops:
        cells = {}
        for name, port in PORTS.items():
            proc = start(name, port)
            try:
                wait_ready(port)
                seed()  # восстановить таблицу: UPDATE/UPSERT/DELETE бьют по id=1
                cells[name] = best_of(
                    port, lambda p: ab_write(p, path, method, body, WRITE_REQUESTS)
                )
            finally:
                proc.terminate()
                proc.wait()
        row = " | ".join(f"{cells[name]:>12,.0f}" for name in names)
        print(f"{op:>8} | {row}")


def main() -> None:
    seed()
    run_select()
    run_writes()


if __name__ == "__main__":
    main()
