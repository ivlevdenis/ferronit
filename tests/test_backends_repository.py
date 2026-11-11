"""Full repository cycle on PostgreSQL, MySQL and SQLite against real DBMSes.

The same data-layer code is exercised: ``create_pool(dsn)`` → ``uow()`` →
``RawRepository`` (dict) and ``RawModelRepository`` (models + relations + aggregates).
Only the DDL and DSN differ:

* PostgreSQL — container on :5432 (the same one as in the data-layer tests);
* SQLite — in-process library (``sqlite://:memory:``), no container needed;
* MySQL — container ``ferronit-mysql`` on :3307 (``./scripts/dbs_up.sh up``).

No driver or no container — the test is skipped. The key MySQL difference
checked here: it has no ``RETURNING``, so a write goes in two steps
(key from the driver + a point read).
"""

from __future__ import annotations

import os
from typing import Annotated

import pytest

from ferronit.contrib import sql
from ferronit.contrib.rawdb import Condition, create_pool
from ferronit.contrib.rawmodel import Model, Relation, select

try:
    import aiosqlite
except ImportError:  # pragma: no cover - extra sqlite is not installed
    aiosqlite = None

try:
    import aiomysql
except ImportError:  # pragma: no cover - extra mysql is not installed
    aiomysql = None


PG_DSN = os.environ.get(
    "FERRONIT_TEST_PG_DSN", "postgresql://postgres:postgres@localhost:5432/postgres"
)
MYSQL_DSN = os.environ.get(
    "FERRONIT_TEST_MYSQL_DSN", "mysql://root:root@127.0.0.1:3307/ferronit_test"
)
SQLITE_DSN = "sqlite://:memory:"

ENGINES = ("postgres", "mysql", "sqlite")

DDL: dict[str, list[str]] = {
    "postgres": [
        "DROP TABLE IF EXISTS ast_items",
        "DROP TABLE IF EXISTS ast_parents",
        "DROP TABLE IF EXISTS ast_users",
        "CREATE TABLE ast_users ("
        "id serial PRIMARY KEY, name text NOT NULL, email text NOT NULL UNIQUE)",
        "CREATE TABLE ast_parents (id serial PRIMARY KEY, title text NOT NULL)",
        "CREATE TABLE ast_items ("
        "id serial PRIMARY KEY, parent_id int NOT NULL, name text NOT NULL)",
    ],
    "mysql": [
        "DROP TABLE IF EXISTS ast_items",
        "DROP TABLE IF EXISTS ast_parents",
        "DROP TABLE IF EXISTS ast_users",
        "CREATE TABLE ast_users ("
        "id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,"
        "name VARCHAR(64) NOT NULL, email VARCHAR(64) NOT NULL UNIQUE)",
        "CREATE TABLE ast_parents ("
        "id INT NOT NULL AUTO_INCREMENT PRIMARY KEY, title VARCHAR(64) NOT NULL)",
        "CREATE TABLE ast_items ("
        "id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,"
        "parent_id INT NOT NULL, name VARCHAR(64) NOT NULL)",
    ],
    "sqlite": [
        "DROP TABLE IF EXISTS ast_items",
        "DROP TABLE IF EXISTS ast_parents",
        "DROP TABLE IF EXISTS ast_users",
        "CREATE TABLE ast_users ("
        "id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE)",
        "CREATE TABLE ast_parents (id INTEGER PRIMARY KEY, title TEXT NOT NULL)",
        "CREATE TABLE ast_items ("
        "id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL, name TEXT NOT NULL)",
    ],
}


class AstUser(Model):
    __table__ = "ast_users"
    id: int = 0
    name: str = ""
    email: str = ""


class AstItem(Model):
    __table__ = "ast_items"
    id: int = 0
    parent_id: int = 0
    name: str = ""


class AstParent(Model):
    __table__ = "ast_parents"
    id: int = 0
    title: str = ""
    items: Annotated[list[AstItem], Relation("parent_id")] = []


def _dsn(engine: str) -> str:
    if engine == "sqlite":
        return SQLITE_DSN
    return MYSQL_DSN if engine == "mysql" else PG_DSN


async def _pool(engine: str):
    """Open a pool to the engine or skip the test if it is missing."""
    if engine == "sqlite" and aiosqlite is None:
        pytest.skip("aiosqlite is not installed (sqlite extra)")
    if engine == "mysql" and aiomysql is None:
        pytest.skip("aiomysql is not installed (mysql extra)")
    try:
        return await create_pool(_dsn(engine))
    except Exception as exc:
        pytest.skip(f"{engine} is unavailable ({exc}); start it with ./scripts/dbs_up.sh up")


async def _prepare(engine: str, pool) -> None:
    """Create the test schema (the DDL is written by hand: the layer has no schema generation)."""
    async with pool.uow() as uow:
        for statement in DDL[engine]:
            await uow.connection.execute(statement)


# ── dict repository ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("engine", ENGINES)
async def test_dict_repository_roundtrip(engine: str) -> None:
    """save → get → list with Condition → update → delete on the dict repository."""
    pool = await _pool(engine)
    try:
        await _prepare(engine, pool)
        async with pool.uow() as uow:
            repo = uow["ast_users"]
            saved = await repo.save({"name": "Ann", "email": "ann@example.com"})
            assert saved["id"] == 1  # the driver returned the key, not RETURNING

            full = await repo.save({"name": "Bob", "email": "bob@example.com"}, returning=True)
            assert full["id"] == 2

            assert await repo.get(1) == {
                "id": 1,
                "name": "Ann",
                "email": "ann@example.com",
            }
            names = [row["name"] for row in await repo.list(limit=10)]
            assert names == ["Ann", "Bob"]

            # Condition with $N: placeholders are reindexed for the engine dialect
            filtered = await repo.list(Condition("name = $1", ["Bob"]))
            assert [row["id"] for row in filtered] == [2]

            updated = await repo.update(2, {"name": "Bob-2"})
            assert updated == {"name": "Bob-2", "id": 2}
            assert (await repo.get(2))["name"] == "Bob-2"

            await repo.delete(1)
            assert await repo.get(1) is None
    finally:
        await pool.close()


# ── model repository ────────────────────────────────────────────────────────


@pytest.mark.parametrize("engine", ENGINES)
async def test_model_repository_roundtrip(engine: str) -> None:
    """save/get/list/upsert/update/save_many/delete_by on models."""
    pool = await _pool(engine)
    try:
        await _prepare(engine, pool)
        async with pool.uow() as uow:
            repo = uow.model(AstUser)

            saved = await repo.save(AstUser(name="Ann", email="ann@example.com"))
            assert saved == AstUser(id=1, name="Ann", email="ann@example.com")
            assert await repo.get(1) == saved

            # upsert with a client key: we take a deliberately larger id because
            # an explicit id insert does not advance the sequence in PostgreSQL (but
            # does in MySQL/SQLite) — otherwise the engines need different expectations.
            assert await repo.upsert(AstUser(id=1000, name="Bob", email="bob@example.com")) == (
                AstUser(id=1000, name="Bob", email="bob@example.com")
            )
            assert await repo.upsert(
                AstUser(id=1000, name="Bob-2", email="bob@example.com")
            ) == AstUser(id=1000, name="Bob-2", email="bob@example.com")

            listed = await repo.list(limit=10)
            assert [user.name for user in listed] == ["Ann", "Bob-2"]
            filtered = await repo.list(Condition("name = $1", ["Ann"]))
            assert [user.id for user in filtered] == [1]

            updated = await repo.update(1, {"email": "ann2@example.com"})
            assert updated == AstUser(id=1, name="Ann", email="ann2@example.com")

            # empty fetch with a filter: the WHERE branch, not _list_sql
            assert await repo.list(Condition("name = $1", ["nobody"])) == []
            assert await repo.fetch_value(select(AstUser).count()) == 2

            many = await repo.save_many(
                [
                    AstUser(name="Cid", email="cid@example.com"),
                    AstUser(name="Dan", email="dan@example.com"),
                ]
            )
            assert len(many) == 2
            assert all(user.id > 1 for user in many)  # keys came from the engine

            await repo.delete_by("name", "Dan")
            assert sorted(user.name for user in await repo.list(limit=10)) == [
                "Ann",
                "Bob-2",
                "Cid",
            ]
    finally:
        await pool.close()


@pytest.mark.parametrize("engine", ENGINES)
async def test_model_repository_query_and_dsl(engine: str) -> None:
    """The Query/DSL path compiles for the connection's engine and executes."""
    pool = await _pool(engine)
    try:
        await _prepare(engine, pool)
        async with pool.uow() as uow:
            repo = uow.model(AstUser)
            await repo.save_many(
                [
                    AstUser(name="Ann", email="a@example.com"),
                    AstUser(name="Bob", email="b@example.com"),
                    AstUser(name="Ann-2", email="c@example.com"),
                ]
            )
            query = select(AstUser).where("name ilike 'ann%'").order_by(AstUser.c.name).limit(5)
            found = await repo.fetch(query)
            assert [user.name for user in found] == ["Ann", "Ann-2"]

            assert await repo.fetch_one(
                select(AstUser).where(AstUser.c.name == "Bob")
            ) == AstUser(id=2, name="Bob", email="b@example.com")
    finally:
        await pool.close()


@pytest.mark.parametrize("engine", ENGINES)
async def test_model_repository_relations(engine: str) -> None:
    """Relations: list() loads children in one query (ANY on Postgres, IN on MySQL)."""
    pool = await _pool(engine)
    try:
        await _prepare(engine, pool)
        async with pool.uow() as uow:
            parents = uow.model(AstParent)
            first = await parents.save(AstParent(title="first"))
            second = await parents.save(AstParent(title="second"))
            items = uow.model(AstItem)
            await items.save_many(
                [
                    AstItem(parent_id=first.id, name="a"),
                    AstItem(parent_id=first.id, name="b"),
                    AstItem(parent_id=second.id, name="c"),
                ]
            )

        async with pool.uow(readonly=True) as uow:
            parents = uow.model(AstParent)
            loaded = await parents.list(limit=10)
            assert [item.name for item in loaded[0].items] == ["a", "b"]
            assert [item.name for item in loaded[1].items] == ["c"]

            point = await parents.get(first.id)
            assert [item.name for item in point.items] == ["a", "b"]
    finally:
        await pool.close()


@pytest.mark.parametrize("engine", ENGINES)
async def test_unit_of_work_rolls_back(engine: str) -> None:
    """An exception inside uow rolls back the write — the same on all engines."""
    pool = await _pool(engine)
    try:
        await _prepare(engine, pool)
        with pytest.raises(RuntimeError):
            async with pool.uow() as uow:
                await uow.model(AstUser).save(AstUser(name="Ghost", email="g@example.com"))
                raise RuntimeError("boom")

        async with pool.uow(readonly=True) as uow:
            assert await uow.model(AstUser).list() == []
    finally:
        await pool.close()


@pytest.mark.parametrize("engine", ENGINES)
async def test_grouped_aggregates(engine: str) -> None:
    """DISTINCT, GROUP BY, aggregates in HAVING and projections — on three engines."""
    pool = await _pool(engine)
    try:
        await _prepare(engine, pool)
        async with pool.uow() as uow:
            repo = uow.model(AstUser)
            await repo.save_many(
                [
                    AstUser(name="Ann", email="a1@example.com"),
                    AstUser(name="Ann", email="a2@example.com"),
                    AstUser(name="Bob", email="b1@example.com"),
                ]
            )

            grouped = await repo.fetch_rows(
                select(AstUser)
                .columns("name", sql.Func("count", "*"))
                .group_by(AstUser.c.name)
                .having(sql.Compare(sql.Func("count", "*"), ">", 1))
                .order_by(AstUser.c.name)
            )
            assert [tuple(row) for row in grouped] == [("Ann", 2)]

            # access to columns by name, not only by index
            assert grouped[0]["name"] == "Ann"

            distinct = await repo.fetch_rows(
                select(AstUser).columns("name").distinct().order_by("name")
            )
            assert [row[0] for row in distinct] == ["Ann", "Bob"]

            total = await repo.fetch_value(
                select(AstUser).aggregate("count(*)").where(AstUser.c.name == "Ann")
            )
            assert total == 2
    finally:
        await pool.close()


@pytest.mark.parametrize("engine", ENGINES)
async def test_explicit_join_kinds(engine: str) -> None:
    """INNER/LEFT/RIGHT/FULL JOIN in the tree — on three real engines.

    The data is chosen so that the join kinds differ in result: ``p2``
    has no children (``INNER`` loses it) and a child has no parent (``LEFT``
    loses it). The MySQL dialect must reject ``FULL``: the server has no
    ``FULL OUTER JOIN``.
    """
    pool = await _pool(engine)
    try:
        await _prepare(engine, pool)
        async with pool.uow() as uow:
            parents = uow.model(AstParent)
            items = uow.model(AstItem)
            await parents.save(AstParent(title="p1"))
            await parents.save(AstParent(title="p2"))
            await items.save_many(
                [
                    AstItem(parent_id=1, name="a"),
                    AstItem(parent_id=999, name="orphan"),
                ]
            )

            def query(kind: str):
                return select(AstParent).join(AstItem, fk="parent_id", kind=kind)

            inner = await parents.fetch_joined(query(sql.INNER))
            assert [p.title for p, _kids in inner] == ["p1"]

            left = await parents.fetch_joined(query(sql.LEFT))
            assert sorted(p.title for p, _kids in left) == ["p1", "p2"]
            parent1, children1 = next(pair for pair in left if pair[0].title == "p1")
            assert [child.name for child in children1] == ["a"]

            if engine == "mysql":
                with pytest.raises(sql.DialectError):
                    query(sql.FULL).compile(sql.MYSQL)
            else:
                right = await parents.fetch_joined(query(sql.RIGHT))
                assert [p.title for p, _kids in right if p.title is not None] == ["p1"]
                assert any(p.id is None for p, _kids in right)

                full = await parents.fetch_joined(query(sql.FULL))
                assert sorted(p.title for p, _kids in full if p.title) == ["p1", "p2"]
                assert any(p.id is None for p, _kids in full)
    finally:
        await pool.close()
