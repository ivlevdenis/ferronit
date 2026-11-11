"""Real DBMSes under generated SQL: SQLite and MySQL.

We check execution, not the string: the SQL that `sql` emits for a dialect
goes to a real database and brings back the expected rows.

* SQLite — an in-process library (no container needed), the `aiosqlite` driver.
* MySQL — the `ferronit-mysql` container on :3307, the `aiomysql` driver:
  `./scripts/dbs_up.sh up`. Without a container the test is skipped, as are the
  postgres tests of the data layer in tests/test_rawdb.py.

DDL is written by hand: the data layer does not generate CREATE TABLE (that is a
separate plan item — migrations). Here exactly what the dialect already supports is checked.
"""

from __future__ import annotations

import os

import pytest

from ferronit.contrib import sql
from ferronit.contrib.rawmodel import Model, select

try:
    import aiosqlite
except ImportError:  # pragma: no cover - sqlite extra not installed
    aiosqlite = None

try:
    import aiomysql
except ImportError:  # pragma: no cover - mysql extra not installed
    aiomysql = None


MYSQL_PARAMS = {
    "host": os.environ.get("FERRONIT_TEST_MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("FERRONIT_TEST_MYSQL_PORT", "3307")),
    "user": os.environ.get("FERRONIT_TEST_MYSQL_USER", "root"),
    "password": os.environ.get("FERRONIT_TEST_MYSQL_PASSWORD", "root"),
    "db": os.environ.get("FERRONIT_TEST_MYSQL_DB", "ferronit_test"),
}

DDL_SQLITE = (
    'CREATE TABLE "ast_users" ('
    '"id" INTEGER PRIMARY KEY, "name" TEXT NOT NULL, "email" TEXT NOT NULL UNIQUE)'
)
DDL_MYSQL = (
    "CREATE TABLE `ast_users` ("
    "`id` INT NOT NULL AUTO_INCREMENT PRIMARY KEY,"
    "`name` VARCHAR(64) NOT NULL, `email` VARCHAR(64) NOT NULL UNIQUE)"
)


class User(Model):
    """Row model for exercising Query/DSL paths on SQLite."""

    id: int = 0
    name: str = ""
    email: str = ""


# ── SQLite ───────────────────────────────────────────────────────────────────


@pytest.mark.skipif(aiosqlite is None, reason="aiosqlite is not installed (sqlite extra)")
async def test_sqlite_executes_generated_sql() -> None:
    async with aiosqlite.connect(":memory:") as db:
        await db.execute(DDL_SQLITE)

        insert_sql, _ = sql.compile_statement(
            sql.Insert("ast_users", ["name", "email"], returning=("id", "name")),
            sql.SQLITE,
        )
        async with db.execute(insert_sql, ["Ann", "ann@example.com"]) as cursor:
            assert await cursor.fetchall() == [(1, "Ann")]

        upsert_sql, _ = sql.compile_statement(
            sql.Insert("ast_users", ["id", "name", "email"], conflict_key="id"),
            sql.SQLITE,
        )
        await db.execute(upsert_sql, [1, "Ann-2", "ann2@example.com"])
        await db.execute(upsert_sql, [2, "Bob", "bob@example.com"])

        select_sql, params = sql.compile_statement(
            sql.Select(
                "ast_users",
                ("id", "name", "email"),
                where=[sql.Compare("name", "=", "Ann-2")],
            ),
            sql.SQLITE,
        )
        async with db.execute(select_sql, params) as cursor:
            assert await cursor.fetchall() == [(1, "Ann-2", "ann2@example.com")]

        in_sql, in_params = sql.compile_statement(
            sql.Select(
                "ast_users",
                ("name",),
                where=[sql.InList("name", ["Ann-2", "Bob"])],
                order=["name"],
            ),
            sql.SQLITE,
        )
        assert in_sql.count("?") == 2
        async with db.execute(in_sql, in_params) as cursor:
            assert await cursor.fetchall() == [("Ann-2",), ("Bob",)]

        update_sql, _ = sql.compile_statement(
            sql.Update("ast_users", ["email"], "id", returning=("id", "email")),
            sql.SQLITE,
        )
        async with db.execute(update_sql, ["ann3@example.com", 1]) as cursor:
            assert await cursor.fetchall() == [(1, "ann3@example.com")]

        delete_sql, _ = sql.compile_statement(sql.Delete("ast_users", "name"), sql.SQLITE)
        await db.execute(delete_sql, ["Bob"])

        count_sql, _ = sql.compile_statement(
            sql.Select("ast_users", ("count(*)",)), sql.SQLITE
        )
        async with db.execute(count_sql) as cursor:
            assert await cursor.fetchone() == (1,)


@pytest.mark.skipif(aiosqlite is None, reason="aiosqlite is not installed (sqlite extra)")
async def test_sqlite_runs_query_and_dsl_path() -> None:
    """The same path as the application: Query/DSL → dialect → database."""
    async with aiosqlite.connect(":memory:") as db:
        await db.execute(DDL_SQLITE.replace("ast_users", "users"))
        users = [("Ann", "ann@example.com"), ("Bob", "bob@example.com")]
        for name, email in users:
            await db.execute('INSERT INTO "users" ("name", "email") VALUES (?, ?)', [name, email])

        query = (
            select(User, dialect=sql.SQLITE)
            .where("name ilike 'a%'")
            .order_by(User.c.name)
            .limit(5)
        )
        sql_text, params = query.compile()
        async with db.execute(sql_text, params) as cursor:
            assert await cursor.fetchall() == [(1, "Ann", "ann@example.com")]

        paged = select(User, dialect=sql.SQLITE).order_by(User.c.id.desc()).limit(1).offset(1)
        sql_text, params = paged.compile()
        async with db.execute(sql_text, params) as cursor:
            assert await cursor.fetchall() == [(1, "Ann", "ann@example.com")]


# ── MySQL ────────────────────────────────────────────────────────────────────


async def _mysql_connect():
    """Connect to the MySQL container or skip the test."""
    if aiomysql is None:
        pytest.skip("aiomysql is not installed (mysql extra)")
    try:
        return await aiomysql.connect(**MYSQL_PARAMS, autocommit=True, connect_timeout=5)
    except Exception as exc:
        # any connection error means there is no container (as in the postgres data-layer tests)
        pytest.skip(f"MySQL is unavailable ({exc}); start it with ./scripts/dbs_up.sh up")


async def test_mysql_executes_generated_sql() -> None:
    conn = await _mysql_connect()
    try:
        async with conn.cursor() as cursor:
            await cursor.execute("DROP TABLE IF EXISTS `ast_users`")
            await cursor.execute(DDL_MYSQL)

            insert_sql, _ = sql.compile_statement(
                sql.Insert("ast_users", ["name", "email"]), sql.MYSQL
            )
            await cursor.execute(insert_sql, ["Ann", "ann@example.com"])
            assert cursor.lastrowid == 1

            # upsert: a PK conflict updates the row instead of adding a second one
            upsert_sql, _ = sql.compile_statement(
                sql.Insert(
                    "ast_users",
                    ["id", "name", "email"],
                    conflict_key="id",
                    update_columns=["name", "email"],
                ),
                sql.MYSQL,
            )
            await cursor.execute(upsert_sql, [1, "Ann-2", "ann2@example.com"])
            await cursor.execute(upsert_sql, [2, "Bob", "bob@example.com"])

            select_sql, params = sql.compile_statement(
                sql.Select(
                    "ast_users",
                    ("id", "name", "email"),
                    where=[sql.Compare("name", "=", "Ann-2")],
                ),
                sql.MYSQL,
            )
            await cursor.execute(select_sql, params)
            assert await cursor.fetchall() == ((1, "Ann-2", "ann2@example.com"),)

            # IN list: MySQL has no ANY — expands to IN (%s, %s)
            in_sql, in_params = sql.compile_statement(
                sql.Select(
                    "ast_users",
                    ("name",),
                    where=[sql.InList("name", ["Ann-2", "Bob"])],
                    order=["name"],
                ),
                sql.MYSQL,
            )
            assert in_sql.count("%s") == 2
            await cursor.execute(in_sql, in_params)
            assert await cursor.fetchall() == (("Ann-2",), ("Bob",))

            # case-insensitive LIKE: no ILIKE, but a *_ci collation gives the same result
            like_sql, like_params = sql.compile_expr(
                sql.Like("name", "b%", insensitive=True), sql.MYSQL
            )
            await cursor.execute(
                f"SELECT `name` FROM `ast_users` WHERE {like_sql}", like_params
            )
            assert await cursor.fetchall() == (("Bob",),)

            update_sql, _ = sql.compile_statement(
                sql.Update("ast_users", ["email"], "id"), sql.MYSQL
            )
            await cursor.execute(update_sql, ["ann3@example.com", 1])

            paged_sql, paged_params = sql.compile_statement(
                sql.Select("ast_users", ("name",), order=["name"], limit=1, offset=1),
                sql.MYSQL,
            )
            await cursor.execute(paged_sql, paged_params)
            assert await cursor.fetchall() == (("Bob",),)

            delete_sql, _ = sql.compile_statement(
                sql.Delete("ast_users", "name"), sql.MYSQL
            )
            await cursor.execute(delete_sql, ["Bob"])

            await cursor.execute("SELECT COUNT(*) FROM `ast_users`")
            assert await cursor.fetchone() == (1,)
    finally:
        conn.close()


async def test_mysql_rejects_returning() -> None:
    """MySQL has no RETURNING — the builder must fail instead of emitting garbage."""
    conn = await _mysql_connect()
    try:
        with pytest.raises(sql.DialectError):
            sql.compile_statement(
                sql.Insert("ast_users", ["name"], returning=("id",)), sql.MYSQL
            )
    finally:
        conn.close()


# ── DSN parsing ─────────────────────────────────────────────────────────

def test_parse_dsn_splits_network_engines() -> None:
    """mysql:// and postgresql:// produce driver arguments with their own default ports."""
    from ferronit.contrib.drivers import parse_dsn

    assert parse_dsn("mysql://root:secret@db.internal:3307/app") == {
        "host": "db.internal",
        "port": 3307,
        "user": "root",
        "password": "secret",
        "db": "app",
    }
    assert parse_dsn("mysql://127.0.0.1/app")["port"] == 3306
    assert parse_dsn("postgresql://postgres@127.0.0.1/db")["port"] == 5432


def test_parse_dsn_gives_sqlite_a_path_not_a_host() -> None:
    """SQLite is a file: take the path verbatim, unlike MySQL (otherwise the database moves)."""
    from ferronit.contrib.drivers import parse_dsn

    assert parse_dsn("sqlite://:memory:") == {"path": ":memory:"}
    assert parse_dsn("sqlite:///var/lib/app.db") == {"path": "/var/lib/app.db"}
    assert parse_dsn("sqlite://") == {"path": ":memory:"}


def test_parse_dsn_rejects_unknown_scheme() -> None:
    """An unknown scheme is an error, not silent MySQL defaults."""
    from ferronit.contrib.drivers import parse_dsn

    with pytest.raises(ValueError):
        parse_dsn("oracle://user@host/db")
    with pytest.raises(ValueError):
        parse_dsn("no-scheme-here")
