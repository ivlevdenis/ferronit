"""Tests for the thin asyncpg adapter (``ferronit.contrib.rawdb``).

The unit part (placeholder reindexing) works without a database. The integration part
requires a PostgreSQL container on :5432 and is skipped if it is absent:

    docker run -d --name ferronit-pg -e POSTGRES_PASSWORD=postgres \\
        -e POSTGRES_USER=postgres -e POSTGRES_DB=postgres -p 5432:5432 postgres:18
"""

from __future__ import annotations

import pytest

from ferronit.contrib import sql
from ferronit.contrib.rawdb import Condition, RawRepository, RawUnitOfWork, create_raw_pool

try:  # asyncpg lives in the "postgres" extra
    import asyncpg
except ModuleNotFoundError:  # pragma: no cover - environment without the extra
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

pytestmark = pytest.mark.skipif(asyncpg is None, reason="asyncpg is not installed (postgres extra)")


# ── unit part: merging conditions needs no database ──────────────────────────


def test_condition_without_params() -> None:
    cond = Condition("active = true")
    assert cond.sql == "active = true"
    assert cond.params == []


def test_merge_filters_renumbers_placeholders() -> None:
    from ferronit.contrib.rawdb import _merge_filters

    where, params = _merge_filters(
        [Condition("age > $1", [18]), Condition("name <> $1 AND age < $2", ["x", 99])]
    )
    assert where == "(age > $1) AND (name <> $2 AND age < $3)"
    assert params == [18, "x", 99]


def test_merge_filters_keeps_two_digit_placeholders() -> None:
    """$1 must not overwrite the start of $10 — otherwise parameters shift."""
    from ferronit.contrib.rawdb import _merge_filters

    where, params = _merge_filters(
        [Condition("a = $1", [1]), Condition("b = $10", list(range(10)))]
    )
    assert where == "(a = $1) AND (b = $11)"
    assert params == [1, *range(10)]


def test_merge_filters_empty() -> None:
    from ferronit.contrib.rawdb import _merge_filters

    assert _merge_filters([]) == ("", [])


# ── integration part ─────────────────────────────────────────────────────────


@pytest.fixture
async def pool():
    """Pool to a real PostgreSQL; tests are skipped without a container."""
    try:
        created = await create_raw_pool(DSN, min_size=1, max_size=4)
    except Exception as exc:  # pragma: no cover - depends on the environment
        pytest.skip(f"PostgreSQL is unavailable: {type(exc).__name__}")

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


async def test_create_pool_wrapper_roundtrip():
    """Pool is a wrapper: create_pool → uow() (write/read) → close()."""
    from ferronit.contrib.rawdb import Pool, create_pool

    try:
        wrapped = await create_pool(DSN, min_size=1, max_size=2)
    except Exception as exc:  # pragma: no cover - depends on the environment
        pytest.skip(f"PostgreSQL is unavailable: {type(exc).__name__}")

    assert isinstance(wrapped, Pool)
    async with wrapped.uow() as uow:
        await uow.connection.execute(DDL)
        row = await uow[TABLE].save({"name": "pool", "email": "p@example.com", "age": 5})

    assert row["name"] == "pool"
    async with wrapped.uow(readonly=True) as uow:
        got = await uow[TABLE].get(row["id"])
    assert got["name"] == "pool"

    await wrapped.close()


async def test_save_returns_generated_pk(repo) -> None:
    row = await repo.save({"name": "A", "email": "a@example.com", "age": 30})
    assert row["id"] == 1
    assert row["name"] == "A"


async def test_save_returning_reads_server_row(repo) -> None:
    row = await repo.save({"name": "B", "email": "b@example.com"}, returning=True)
    assert row["id"] == 1
    assert row["age"] == 0  # server default is visible only through RETURNING


async def test_get_and_missing(repo) -> None:
    saved = await repo.save({"name": "C", "email": "c@example.com"})
    fetched = await repo.get(saved["id"])
    assert fetched is not None
    # the fast save() path does not read the row from the server, so the response has no
    # columns with a server default — they appear when reading from the database
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
    """readonly=True does not open BEGIN/COMMIT — reads need no transaction."""
    async with RawUnitOfWork(pool, readonly=True) as uow:
        assert not uow.connection.is_in_transaction()
        await uow[TABLE].list()
    async with RawUnitOfWork(pool) as uow:
        assert uow.connection.is_in_transaction()


async def test_same_rows_as_core_repository(pool) -> None:
    """The rawdb repository returns exactly what was saved (dict contract)."""
    reference = {"name": "same", "email": "same@example.com", "age": 33}
    async with RawUnitOfWork(pool) as uow:
        saved = await uow[TABLE].save(dict(reference))
        fetched = await uow[TABLE].get(saved["id"])

    assert fetched == saved


# ── configurable RETURNING (fake connection, no database) ────────────────────
#
# The stub's dialect is set by an attribute, SQL is recorded, and rows are returned
# pre-made — this covers both paths: with RETURNING (PostgreSQL/SQLite) and
# via a follow-up read (MySQL, which has no RETURNING).


class FakeConnection:
    """Connection stub: remembers SQL and returns a pre-made row."""

    def __init__(self, dialect, *, row=None, rows=None, key=7) -> None:
        self.dialect = dialect
        self.row = row
        self.rows = rows or []
        self.key = key
        self.calls: list[tuple[str, str]] = []

    async def execute(self, sql_text: str, *params):
        self.calls.append(("execute", sql_text))
        return self.key

    async def fetchrow(self, sql_text: str, *params):
        self.calls.append(("fetchrow", sql_text))
        return self.row

    async def fetch(self, sql_text: str, *params):
        self.calls.append(("fetch", sql_text))
        if self.rows:
            return self.rows
        return [self.row] if self.row is not None else []

    async def fetchval(self, sql_text: str, *params):
        self.calls.append(("fetchval", sql_text))
        return self.key


async def test_save_asks_for_exactly_the_requested_columns() -> None:
    conn = FakeConnection(sql.POSTGRES, row={"id": 7, "name": "A", "age": 0})
    repo = RawRepository(conn, TABLE)
    row = await repo.save({"name": "A"}, returning=("id", "name"))

    assert conn.calls[-1] == (
        "fetchrow",
        f"INSERT INTO {TABLE} (name) VALUES ($1) RETURNING id, name",
    )
    assert row == {"id": 7, "name": "A"}


async def test_save_returning_true_is_the_whole_row() -> None:
    conn = FakeConnection(sql.POSTGRES, row={"id": 7, "name": "A", "age": 0})
    repo = RawRepository(conn, TABLE)
    row = await repo.save({"name": "A"}, returning=True)

    assert conn.calls[-1][1].endswith("RETURNING *")
    assert row == {"id": 7, "name": "A", "age": 0}


async def test_update_returning_reports_a_missing_row() -> None:
    conn = FakeConnection(sql.POSTGRES, row=None)
    repo = RawRepository(conn, TABLE)
    assert await repo.update(404, {"name": "ghost"}, returning=True) is None
    assert conn.calls[-1] == (
        "fetchrow",
        f"UPDATE {TABLE} SET name = $1 WHERE id = $2 RETURNING *",
    )


async def test_update_without_returning_keeps_the_old_shape() -> None:
    conn = FakeConnection(sql.POSTGRES, row=None)
    repo = RawRepository(conn, TABLE)
    assert await repo.update(1, {"name": "B"}) == {"name": "B", "id": 1}
    assert conn.calls[-1] == ("execute", f"UPDATE {TABLE} SET name = $1 WHERE id = $2")


async def test_delete_returning_gives_the_deleted_row() -> None:
    conn = FakeConnection(sql.POSTGRES, row={"id": 1})
    repo = RawRepository(conn, TABLE)
    assert await repo.delete(1, returning=("id",)) == {"id": 1}
    assert conn.calls[-1] == ("fetchrow", f"DELETE FROM {TABLE} WHERE id = $1 RETURNING id")

    empty = FakeConnection(sql.POSTGRES, row=None)
    assert await RawRepository(empty, TABLE).delete(1, returning=True) is None


async def test_delete_without_returning_stays_a_plain_delete() -> None:
    conn = FakeConnection(sql.POSTGRES, row={"id": 1})
    repo = RawRepository(conn, TABLE)
    assert await repo.delete(1) is None
    assert conn.calls[-1] == ("execute", f"DELETE FROM {TABLE} WHERE id = $1")


async def test_mysql_save_reads_the_row_back_and_projects_it() -> None:
    """MySQL has no RETURNING: the row is read back by a second query."""
    conn = FakeConnection(sql.MYSQL, row={"id": 7, "name": "A", "age": 0})
    repo = RawRepository(conn, TABLE)
    row = await repo.save({"name": "A"}, returning=("name",))

    assert "RETURNING" not in conn.calls[0][1]
    assert row == {"name": "A"}
    assert conn.calls[0][0] == "execute" and conn.calls[1][0] == "fetchrow"


async def test_mysql_update_and_delete_read_back_without_returning() -> None:
    conn = FakeConnection(sql.MYSQL, row={"id": 1, "name": "old"})
    repo = RawRepository(conn, TABLE)
    assert await repo.update(1, {"name": "new"}, returning=("name",)) == {"name": "old"}
    assert [call[0] for call in conn.calls] == ["execute", "fetchrow"]
    assert not any("RETURNING" in call[1] for call in conn.calls)

    conn = FakeConnection(sql.MYSQL, row={"id": 1, "name": "old"})
    repo = RawRepository(conn, TABLE)
    assert await repo.delete(1, returning=True) == {"id": 1, "name": "old"}
    # the pre-image is read BEFORE deletion, otherwise there is nothing to return
    assert [call[0] for call in conn.calls] == ["fetchrow", "execute"]


# ── batch INSERT (save_many) ─────────────────────────────────────────────────


async def test_save_many_splits_into_chunks() -> None:
    conn = FakeConnection(sql.POSTGRES, rows=[{"id": 1}, {"id": 2}])
    repo = RawRepository(conn, TABLE)
    rows = [{"name": f"N{i}", "age": i} for i in range(4)]
    saved = await repo.save_many(rows, chunk_size=2)

    assert len(conn.calls) == 2  # 4 rows with 2 per chunk — two queries, not four
    assert all(call[0] == "fetch" for call in conn.calls)
    assert conn.calls[0][1] == (
        f"INSERT INTO {TABLE} (name, age) VALUES ($1, $2), ($3, $4) RETURNING id"
    )
    assert saved == [
        {"name": "N0", "age": 0, "id": 1},
        {"name": "N1", "age": 1, "id": 2},
        {"name": "N2", "age": 2, "id": 1},
        {"name": "N3", "age": 3, "id": 2},
    ]


async def test_save_many_chunk_default_follows_the_parameter_limit() -> None:
    """Chunk size = placeholder limit / number of columns (SQLite: 32 766 / 3)."""
    conn = FakeConnection(sql.SQLITE, row=None)
    repo = RawRepository(conn, TABLE)
    rows = [{"id": i, "a": 1, "b": 2} for i in range(16_384)]
    await repo.save_many(rows)

    first, second = conn.calls  # id in the data → no RETURNING, only execute
    expected = sql.SQLITE.max_parameters // 3
    assert first[0] == "execute" and first[1].count("(?, ?, ?)") == expected
    assert second[1].count("(?, ?, ?)") == 16_384 - expected


async def test_save_many_returning_columns_are_projected() -> None:
    conn = FakeConnection(sql.POSTGRES, rows=[{"id": 5, "name": "Z", "age": 9}])
    repo = RawRepository(conn, TABLE)
    saved = await repo.save_many([{"name": "Z", "age": 9}], returning=("id", "name"))

    assert conn.calls[-1][1].endswith("RETURNING id, name")
    assert saved == [{"id": 5, "name": "Z"}]


async def test_save_many_rejects_rows_with_different_columns() -> None:
    conn = FakeConnection(sql.POSTGRES, row={"id": 1})
    repo = RawRepository(conn, TABLE)
    with pytest.raises(ValueError):
        await repo.save_many([{"name": "A"}, {"age": 1}])


async def test_save_many_is_a_no_op_for_no_rows() -> None:
    conn = FakeConnection(sql.POSTGRES, row={"id": 1})
    assert await RawRepository(conn, TABLE).save_many([]) == []
    assert conn.calls == []


async def test_save_many_on_mysql_without_returning_is_one_statement() -> None:
    conn = FakeConnection(sql.MYSQL, row={"id": 1})
    repo = RawRepository(conn, TABLE)
    saved = await repo.save_many([{"name": "A"}, {"name": "B"}])

    assert conn.calls == [
        ("execute", f"INSERT INTO `{TABLE}` (name) VALUES (%s), (%s)")
    ]
    # MySQL has no RETURNING — do not invent keys
    assert saved == [{"name": "A"}, {"name": "B"}]


async def test_save_many_on_mysql_with_returning_falls_back_per_row() -> None:
    conn = FakeConnection(sql.MYSQL, row={"id": 1, "name": "A"})
    repo = RawRepository(conn, TABLE)
    saved = await repo.save_many([{"name": "A"}, {"name": "B"}], returning=("name",))

    assert saved == [{"name": "A"}, {"name": "A"}]
    # for each row: INSERT + follow-up read
    assert [call[0] for call in conn.calls] == ["execute", "fetchrow", "execute", "fetchrow"]


# ── connection return-to-pool policy ─────────────────────────────────────────


class _FakeAsyncpg:
    """Minimal stand-in exposing ``create_pool`` with (or without) extra kwargs."""

    def __init__(self, **parameters: object) -> None:
        async def create_pool(dsn, **kwargs):
            return kwargs

        create_pool.__name__ = "create_pool"
        create_pool.__signature__ = _signature(**parameters)  # type: ignore[attr-defined]
        self.create_pool = create_pool


def _signature(**parameters: object):
    import inspect

    return inspect.Signature(
        [inspect.Parameter("dsn", inspect.Parameter.POSITIONAL_OR_KEYWORD)]
        + [
            inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY)
            for name in parameters
        ]
    )


def test_pool_kwargs_fast_path_when_driver_supports_reset() -> None:
    from ferronit.contrib.rawdb import _asyncpg_pool_kwargs, _no_session_reset

    fake = _FakeAsyncpg(reset=None, max_inactive_connection_lifetime=300)
    kwargs = _asyncpg_pool_kwargs(fake, reset_on_release=False)

    assert kwargs["reset"] is _no_session_reset
    assert kwargs["max_inactive_connection_lifetime"] == 0


def test_pool_kwargs_keeps_driver_defaults_when_reset_requested() -> None:
    from ferronit.contrib.rawdb import _asyncpg_pool_kwargs

    fake = _FakeAsyncpg(reset=None, max_inactive_connection_lifetime=300)
    assert _asyncpg_pool_kwargs(fake, reset_on_release=True) == {}


def test_pool_kwargs_does_not_pass_unknown_reset_keyword() -> None:
    """asyncpg < 0.30: ``reset`` is unknown — it would leak into ``connect(**kwargs)``."""
    from ferronit.contrib.rawdb import _asyncpg_pool_kwargs

    fake = _FakeAsyncpg(max_inactive_connection_lifetime=300)
    kwargs = _asyncpg_pool_kwargs(fake, reset_on_release=False)

    assert "reset" not in kwargs
    assert kwargs["max_inactive_connection_lifetime"] == 0


@pytest.fixture
async def single_conn_pool():
    """Single-connection pool: the next uow takes the same session."""
    try:
        created = await create_raw_pool(DSN, min_size=1, max_size=1)
    except Exception as exc:  # pragma: no cover - depends on the environment
        pytest.skip(f"PostgreSQL is unavailable: {type(exc).__name__}")
    yield created
    await created.close()


async def test_release_keeps_session_state_by_default(single_conn_pool) -> None:
    """Fast path: a SET in one unit of work is visible to the next on the same connection."""
    async with RawUnitOfWork(single_conn_pool, readonly=True) as uow:
        await uow.connection.execute("SET statement_timeout = '1234ms'")

    async with RawUnitOfWork(single_conn_pool, readonly=True) as uow:
        leaked = await uow.connection.fetchval("SHOW statement_timeout")

    assert leaked == "1234ms"


async def test_release_with_full_reset_clears_session_state() -> None:
    """reset_on_release=True restores asyncpg's previous hygiene (RESET ALL)."""
    try:
        created = await create_raw_pool(DSN, min_size=1, max_size=1, reset_on_release=True)
    except Exception as exc:  # pragma: no cover - depends on the environment
        pytest.skip(f"PostgreSQL is unavailable: {type(exc).__name__}")

    try:
        async with RawUnitOfWork(created, readonly=True) as uow:
            default_timeout = await uow.connection.fetchval("SHOW statement_timeout")
        async with RawUnitOfWork(created, readonly=True) as uow:
            await uow.connection.execute("SET statement_timeout = '1234ms'")

        async with RawUnitOfWork(created, readonly=True) as uow:
            after_reset = await uow.connection.fetchval("SHOW statement_timeout")
    finally:
        await created.close()

    assert after_reset == default_timeout
