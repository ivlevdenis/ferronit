"""Тесты тонкого адаптера на asyncpg (``ferronit.contrib.rawdb``).

Юнит-часть (переиндексация плейсхолдеров) работает без базы. Интеграционная часть
требует контейнер PostgreSQL на :5432 и пропускается, если его нет:

    docker run -d --name ferronit-pg -e POSTGRES_PASSWORD=postgres \\
        -e POSTGRES_USER=postgres -e POSTGRES_DB=postgres -p 5432:5432 postgres:18
"""

from __future__ import annotations

import pytest

from ferronit.contrib import sql
from ferronit.contrib.rawdb import Condition, RawRepository, RawUnitOfWork, create_raw_pool

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
    from ferronit.contrib.rawdb import _merge_filters

    where, params = _merge_filters(
        [Condition("age > $1", [18]), Condition("name <> $1 AND age < $2", ["x", 99])]
    )
    assert where == "(age > $1) AND (name <> $2 AND age < $3)"
    assert params == [18, "x", 99]


def test_merge_filters_keeps_two_digit_placeholders() -> None:
    """$1 не должен затирать начало $10 — иначе параметры уезжают."""
    from ferronit.contrib.rawdb import _merge_filters

    where, params = _merge_filters(
        [Condition("a = $1", [1]), Condition("b = $10", list(range(10)))]
    )
    assert where == "(a = $1) AND (b = $11)"
    assert params == [1, *range(10)]


def test_merge_filters_empty() -> None:
    from ferronit.contrib.rawdb import _merge_filters

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


async def test_create_pool_wrapper_roundtrip():
    """Pool — обёртка: create_pool → uow() (write/read) → close()."""
    from ferronit.contrib.rawdb import Pool, create_pool

    try:
        wrapped = await create_pool(DSN, min_size=1, max_size=2)
    except Exception as exc:  # pragma: no cover - зависит от окружения
        pytest.skip(f"PostgreSQL недоступен: {type(exc).__name__}")

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
    """Репозиторий rawdb отдаёт ровно то, что сохранил (dict-контракт)."""
    reference = {"name": "same", "email": "same@example.com", "age": 33}
    async with RawUnitOfWork(pool) as uow:
        saved = await uow[TABLE].save(dict(reference))
        fetched = await uow[TABLE].get(saved["id"])

    assert fetched == saved


# ── настраиваемый RETURNING (фейковое соединение, база не нужна) ─────────────
#
# Диалект у заглушки задаётся атрибутом, SQL записывается, строки отдаются
# заготовленные — так проверяются оба пути: с RETURNING (PostgreSQL/SQLite) и
# через дочитывание (MySQL, где RETURNING нет).


class FakeConnection:
    """Заглушка соединения: помнит SQL, отдаёт заготовленную строку."""

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
    """У MySQL нет RETURNING: строка дочитывается вторым запросом."""
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
    # пред-образ читается ДО удаления, иначе возвращать нечего
    assert [call[0] for call in conn.calls] == ["fetchrow", "execute"]


# ── батч-INSERT (save_many) ──────────────────────────────────────────────────


async def test_save_many_splits_into_chunks() -> None:
    conn = FakeConnection(sql.POSTGRES, rows=[{"id": 1}, {"id": 2}])
    repo = RawRepository(conn, TABLE)
    rows = [{"name": f"N{i}", "age": i} for i in range(4)]
    saved = await repo.save_many(rows, chunk_size=2)

    assert len(conn.calls) == 2  # 4 строки по 2 — два запроса, а не четыре
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
    """Размер чанка = лимит плейсхолдеров / число колонок (SQLite: 32 766 / 3)."""
    conn = FakeConnection(sql.SQLITE, row=None)
    repo = RawRepository(conn, TABLE)
    rows = [{"id": i, "a": 1, "b": 2} for i in range(16_384)]
    await repo.save_many(rows)

    first, second = conn.calls  # id в данных → никакого RETURNING, только execute
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
    # у MySQL нет RETURNING — ключи не выдумываем
    assert saved == [{"name": "A"}, {"name": "B"}]


async def test_save_many_on_mysql_with_returning_falls_back_per_row() -> None:
    conn = FakeConnection(sql.MYSQL, row={"id": 1, "name": "A"})
    repo = RawRepository(conn, TABLE)
    saved = await repo.save_many([{"name": "A"}, {"name": "B"}], returning=("name",))

    assert saved == [{"name": "A"}, {"name": "A"}]
    # на каждую строку: INSERT + дочитывание
    assert [call[0] for call in conn.calls] == ["execute", "fetchrow", "execute", "fetchrow"]
