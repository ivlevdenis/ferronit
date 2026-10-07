"""Тесты тонкого адаптера на asyncpg (``ferrox.contrib.rawdb``).

Юнит-часть (переиндексация плейсхолдеров) работает без базы. Интеграционная часть
требует контейнер PostgreSQL на :5432 и пропускается, если его нет:

    docker run -d --name ferrox-pg -e POSTGRES_PASSWORD=postgres \\
        -e POSTGRES_USER=postgres -e POSTGRES_DB=postgres -p 5432:5432 postgres:18
"""

from __future__ import annotations

import pytest

from ferrox.contrib.rawdb import Condition, RawRepository, RawUnitOfWork, create_raw_pool

try:  # asyncpg живёт в extra "postgres"
    import asyncpg
except ModuleNotFoundError:  # pragma: no cover - окружение без extra
    asyncpg = None  # type: ignore[assignment]

DSN = "postgresql://postgres:postgres@localhost:5432/postgres"
TABLE = "raw_repo_test"

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id serial PRIMARY KEY,
    name text NOT NULL,
    email text NOT NULL,
    age integer NOT NULL DEFAULT 0
)
"""

pytestmark = pytest.mark.skipif(asyncpg is None, reason="asyncpg не установлен (extra postgres)")


# ── юнит-часть: склейка условий не требует базы ───────────────────────────────


def test_condition_without_params() -> None:
    cond = Condition("active = true")
    assert cond.sql == "active = true"
    assert cond.params == []


def test_merge_filters_renumbers_placeholders() -> None:
    from ferrox.contrib.rawdb import _merge_filters

    where, params = _merge_filters(
        [Condition("age > $1", [18]), Condition("name <> $1 AND age < $2", ["x", 99])]
    )
    assert where == "(age > $1) AND (name <> $2 AND age < $3)"
    assert params == [18, "x", 99]


def test_merge_filters_keeps_two_digit_placeholders() -> None:
    """$1 не должен затирать начало $10 — иначе параметры уезжают."""
    from ferrox.contrib.rawdb import _merge_filters

    where, params = _merge_filters(
        [Condition("a = $1", [1]), Condition("b = $10", list(range(10)))]
    )
    assert where == "(a = $1) AND (b = $11)"
    assert params == [1, *range(10)]


def test_merge_filters_empty() -> None:
    from ferrox.contrib.rawdb import _merge_filters

    assert _merge_filters([]) == ("", [])


# ── интеграционная часть ─────────────────────────────────────────────────────


@pytest.fixture
async def pool():
    """Пул к реальному PostgreSQL; без контейнера тесты пропускаются."""
    try:
        created = await create_raw_pool(DSN, min_size=1, max_size=4)
    except Exception as exc:  # pragma: no cover - зависит от окружения
        pytest.skip(f"PostgreSQL недоступен: {type(exc).__name__}")

    async with created.acquire() as conn:
        await conn.execute(DDL)
        await conn.execute(f"TRUNCATE {TABLE} RESTART IDENTITY")
    yield created
    await created.close()


async def _new_conn(pool) -> asyncpg.Connection:
    return await pool.acquire()


@pytest.fixture
async def repo(pool):
    conn = await _new_conn(pool)
    yield RawRepository(conn, TABLE)
    await pool.release(conn)


async def test_save_returns_generated_pk(repo) -> None:
    row = await repo.save({"name": "A", "email": "a@example.com", "age": 30})
    assert row["id"] == 1
    assert row["name"] == "A"


async def test_save_returning_reads_server_row(repo) -> None:
    row = await repo.save({"name": "B", "email": "b@example.com"}, returning=True)
    assert row["id"] == 1
    assert row["age"] == 0  # server default виден только через RETURNING


async def test_get_and_missing(repo) -> None:
    saved = await repo.save({"name": "C", "email": "c@example.com"})
    fetched = await repo.get(saved["id"])
    assert fetched is not None
    # быстрый путь save() не читает строку с сервера, поэтому в ответе нет
    # колонок с server default — они появятся при чтении из базы
    assert {key: fetched[key] for key in saved} == saved
    assert fetched["age"] == 0
    assert await repo.get(9999) is None


async def test_list_with_filter_and_pagination(repo) -> None:
    for index in range(5):
        await repo.save({"name": f"u{index}", "email": "x@example.com", "age": 20 + index})

    page = await repo.list(Condition("age >= $1", [22]), limit=2)
    assert [row["age"] for row in page] == [22, 23]

    offset_page = await repo.list(limit=2, offset=3)
    assert [row["age"] for row in offset_page] == [23, 24]


async def test_update_and_delete(repo) -> None:
    saved = await repo.save({"name": "D", "email": "d@example.com"})
    updated = await repo.update(saved["id"], {"name": "D2"})
    assert updated["name"] == "D2"
    assert (await repo.get(saved["id"]))["name"] == "D2"

    await repo.delete(saved["id"])
    assert await repo.get(saved["id"]) is None


async def test_list_two_conditions_share_params(repo) -> None:
    await repo.save({"name": "keep", "email": "k@example.com", "age": 40})
    await repo.save({"name": "drop", "email": "d@example.com", "age": 10})

    rows = await repo.list(Condition("age > $1", [30]), Condition("name = $1", ["keep"]))
    assert [row["name"] for row in rows] == ["keep"]


async def test_unit_of_work_commits(pool) -> None:
    uow = RawUnitOfWork(pool)
    async with uow:
        await uow[TABLE].save({"name": "E", "email": "e@example.com"})

    async with RawUnitOfWork(pool) as check:
        assert len(await check[TABLE].list()) == 1


async def test_unit_of_work_rolls_back_on_error(pool) -> None:
    uow = RawUnitOfWork(pool)
    with pytest.raises(RuntimeError):
        async with uow:
            await uow[TABLE].save({"name": "F", "email": "f@example.com"})
            raise RuntimeError("boom")

    async with RawUnitOfWork(pool) as check:
        assert await check[TABLE].list() == []


async def test_unit_of_work_requires_context(pool) -> None:
    uow = RawUnitOfWork(pool)
    with pytest.raises(RuntimeError):
        _ = uow[TABLE]


async def test_unit_of_work_readonly_skips_transaction(pool) -> None:
    """readonly=True не открывает BEGIN/COMMIT — для чтения транзакция не нужна."""
    async with RawUnitOfWork(pool, readonly=True) as uow:
        assert not uow.connection.is_in_transaction()
        await uow[TABLE].list()
    async with RawUnitOfWork(pool) as uow:
        assert uow.connection.is_in_transaction()


async def test_same_rows_as_core_repository(pool) -> None:
    """Ключевая проверка «тот же контракт»: результаты совпадают с SQLAlchemy Core."""
    from sqlalchemy import Column, Integer, MetaData, String, Table
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from ferrox.contrib.db import RelationalUnitOfWork

    metadata = MetaData()
    table = Table(
        TABLE,
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String),
        Column("email", String),
        Column("age", Integer),
    )

    reference = {"name": "same", "email": "same@example.com", "age": 33}
    async with RawUnitOfWork(pool) as uow:
        raw_row = await uow[TABLE].save(dict(reference))

    engine = create_async_engine(DSN.replace("postgresql://", "postgresql+asyncpg://"))
    try:
        core_uow = RelationalUnitOfWork(async_sessionmaker(engine, expire_on_commit=False))
        async with core_uow:
            core_row = await core_uow[table].get(raw_row["id"])
    finally:
        await engine.dispose()

    assert core_row == raw_row
