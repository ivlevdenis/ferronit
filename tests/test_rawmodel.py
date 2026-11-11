"""Tests for declarative models over the raw asyncpg layer (``ferronit.contrib.rawmodel``).

The unit part (table-name pluralization, ``Query`` compilation, ``IdentityMap``, the
repository with a fake connection) works without a database. The integration part
requires a PostgreSQL container on :5432 and is skipped if it is absent:

    docker run -d --name ferronit-pg -e POSTGRES_PASSWORD=postgres \\
        -e POSTGRES_USER=postgres -e POSTGRES_DB=postgres -p 5432:5432 postgres:18
"""

from __future__ import annotations

from typing import Annotated

import pytest

from ferronit.contrib import sql
from ferronit.contrib.rawdb import Pool, RawUnitOfWork, create_raw_pool
from ferronit.contrib.rawmodel import (
    IdentityMap,
    Mapper,
    Model,
    RawModelRepository,
    Relation,
    Repository,
    select,
)

try:  # asyncpg lives in the "postgres" extra
    import asyncpg
except ModuleNotFoundError:  # pragma: no cover - environment without the extra
    asyncpg = None  # type: ignore[assignment]

DSN = "postgresql://postgres:postgres@localhost:5432/postgres"
TABLE = "raw_model_test"

pytestmark = pytest.mark.skipif(asyncpg is None, reason="asyncpg is not installed (postgres extra)")


# ── models for unit tests ────────────────────────────────────────────────────


class User(Model):
    id: int = 0
    name: str = ""
    email: str = ""


class Category(Model):
    id: int = 0
    title: str = ""


class Person(Model):
    id: int = 0
    name: str = ""


class Box(Model):
    id: int = 0


class Dish(Model):
    id: int = 0


class Series(Model):
    id: int = 0


class Widget(Model):
    __table__ = "app_widgets"
    id: int = 0


def _table_of(model: type[Model], table: str | None = None) -> str:
    sql, _ = select(model, table).compile()
    return sql.split(" FROM ", 1)[1].split(" ", 1)[0]


# ── table name: pluralization ────────────────────────────────────────────────


def test_default_table_pluralizes() -> None:
    assert _table_of(User) == "users"
    assert _table_of(Category) == "categories"
    assert _table_of(Person) == "people"
    assert _table_of(Box) == "boxes"
    assert _table_of(Dish) == "dishes"


def test_uncountable_noun_is_left_alone() -> None:
    assert _table_of(Series) == "series"


def test_explicit_table_argument_wins() -> None:
    assert _table_of(User, "app_users") == "app_users"


def test_model_table_attribute_is_used() -> None:
    assert _table_of(Widget) == "app_widgets"


def test_explicit_argument_overrides_model_attribute() -> None:
    assert _table_of(Widget, "other_widgets") == "other_widgets"


# ── Query compilation ────────────────────────────────────────────────────────


def test_column_comparisons_build_conditions() -> None:
    sql, params = (
        select(User)
        .where(User.c.name == "A", User.c.email != "x", User.c.id > 5)
        .compile()
    )
    assert sql == (
        "SELECT id, name, email FROM users"
        " WHERE (name = $1) AND (email <> $2) AND (id > $3)"
    )
    assert params == ["A", "x", 5]


def test_placeholders_continue_after_where() -> None:
    sql, params = select(User).where(User.c.id > 5).limit(10).offset(2).compile()
    assert sql == (
        "SELECT id, name, email FROM users WHERE (id > $1) LIMIT $2 OFFSET $3"
    )
    assert params == [5, 10, 2]


def test_order_by_columns_and_raw_terms() -> None:
    sql, params = select(User).order_by(User.c.name, User.c.id.desc()).compile()
    assert sql == "SELECT id, name, email FROM users ORDER BY name, id DESC"
    assert params == []


def test_or_combine_conditions() -> None:
    sql, params = select(User).where((User.c.id > 1) | (User.c.name == "A")).compile()
    assert sql == "SELECT id, name, email FROM users WHERE ((id > $1) OR (name = $2))"
    assert params == [1, "A"]


def test_and_operator() -> None:
    sql, params = select(User).where((User.c.id > 1) & User.c.email.is_null()).compile()
    assert sql == "SELECT id, name, email FROM users WHERE ((id > $1) AND (email IS NULL))"
    assert params == [1]


def test_invert_operator() -> None:
    sql, params = select(User).where(~User.c.id.in_([1, 2, 3])).compile()
    assert sql == "SELECT id, name, email FROM users WHERE (NOT (id = ANY($1)))"
    assert params == [[1, 2, 3]]


def test_where_dsl_and_or_in() -> None:
    sql, params = select(User).where(
        "id > 1 and (name ilike '%a%' or email in ('x', 'y'))"
    ).compile()
    assert sql == (
        "SELECT id, name, email FROM users "
        "WHERE ((id > $1) AND ((name ILIKE $2) OR (email = ANY($3))))"
    )
    assert params == [1, "%a%", ["x", "y"]]


def test_where_dsl_predicates() -> None:
    sql, params = select(User).where(
        "email is not null and id not in (1, 2) and id between 1 and 3"
    ).compile()
    assert sql == (
        "SELECT id, name, email FROM users "
        "WHERE (((email IS NOT NULL) AND (NOT (id = ANY($1)))) "
        "AND (id BETWEEN $2 AND $3))"
    )
    assert params == [[1, 2], 1, 3]


def test_where_dsl_null_and_errors() -> None:
    sql, params = select(User).where("name != null").compile()
    assert sql == "SELECT id, name, email FROM users WHERE (name IS NOT NULL)"
    assert params == []

    with pytest.raises(ValueError, match="Unknown column"):
        select(User).where("nope > 1")
    with pytest.raises(ValueError):
        select(User).where("age >")


def test_between() -> None:
    sql, params = select(User).where(User.c.id.between(5, 10)).compile()
    assert sql == "SELECT id, name, email FROM users WHERE (id BETWEEN $1 AND $2)"
    assert params == [5, 10]


def test_in_like_is_null() -> None:
    sql, params = (
        select(User)
        .where(User.c.id.in_([1, 2, 3]), User.c.name.ilike("%a%"), User.c.email.is_null())
        .compile()
    )
    assert sql == (
        "SELECT id, name, email FROM users "
        "WHERE (id = ANY($1)) AND (name ILIKE $2) AND (email IS NULL)"
    )
    assert params == [[1, 2, 3], "%a%"]


def test_eq_none_means_is_null() -> None:
    sql, params = select(User).where(User.c.email == None).compile()  # noqa: E711
    assert sql == "SELECT id, name, email FROM users WHERE (email IS NULL)"
    assert params == []


def test_count_aggregate_compile() -> None:
    sql, params = select(User).where(User.c.id > 0).count().compile()
    assert sql == "SELECT count(*) FROM users WHERE (id > $1)"
    assert params == [0]


def test_columns_override() -> None:
    sql, _ = select(User).columns("id", "name").compile()
    assert sql == "SELECT id, name FROM users"


def test_first_adds_limit() -> None:
    sql, params = select(User).first().compile()
    assert sql == "SELECT id, name, email FROM users LIMIT $1"
    assert params == [1]


def test_unknown_column_raises_attribute_error() -> None:
    with pytest.raises(AttributeError):
        _ = User.c.nope


# ── model as a dataclass ─────────────────────────────────────────────────────


def test_columns_capture_declaration_order() -> None:
    assert User.__columns__ == ("id", "name", "email")


def test_from_row_and_to_tuple_roundtrip() -> None:
    user = User.from_row((1, "A", "a@example.com"))
    assert user == User(id=1, name="A", email="a@example.com")
    assert user.to_tuple() == (1, "A", "a@example.com")


def test_identity_map_holds_strong_references() -> None:
    """The cache lives with the unit of work and holds rows until it is discarded."""
    identity_map = IdentityMap()
    user = User.from_row((1, "A", "a@example.com"))
    identity_map.put(User, 1, user)
    del user
    assert identity_map.get(User, 1) == User(id=1, name="A", email="a@example.com")


def test_model_instance_is_unhashable() -> None:
    with pytest.raises(TypeError):
        hash(User(id=1, name="A", email="a@example.com"))


# ── repository on a fake connection ──────────────────────────────────────────


class FakeConnection:
    """Minimal asyncpg connection stub that records calls."""

    def __init__(self, *, row=None, rows=None) -> None:
        self.row = row
        self.rows = rows or []
        self.calls: list[tuple[str, str, tuple]] = []

    async def fetchrow(self, sql: str, *params):
        self.calls.append(("fetchrow", sql, params))
        return self.row

    async def fetch(self, sql: str, *params):
        self.calls.append(("fetch", sql, params))
        return self.rows

    async def execute(self, sql: str, *params):
        self.calls.append(("execute", sql, params))


async def test_repository_uses_pluralized_table_by_default() -> None:
    conn = FakeConnection(row=(1, "A", "a@example.com"))
    repo = RawModelRepository(conn, User)
    await repo.get(1)
    assert conn.calls[-1][1] == "SELECT id, name, email FROM users WHERE id = $1"


async def test_repository_honours_explicit_table() -> None:
    conn = FakeConnection(row=(1, "A", "a@example.com"))
    repo = RawModelRepository(conn, User, table="custom_users")
    await repo.get(1)
    assert "FROM custom_users" in conn.calls[-1][1]


async def test_save_returns_model_with_generated_id() -> None:
    conn = FakeConnection(row=(7, "Zoe", "z@example.com"))
    repo = RawModelRepository(conn, User)
    saved = await repo.save(User(name="Zoe", email="z@example.com"))
    assert saved == User(id=7, name="Zoe", email="z@example.com")
    assert conn.calls[-1][0:1] == ("fetchrow",)
    assert conn.calls[-1][2] == ("Zoe", "z@example.com")  # pk is not sent to INSERT


async def test_list_selects_only_model_columns() -> None:
    conn = FakeConnection(rows=[])
    repo = RawModelRepository(conn, User)
    await repo.list()
    assert conn.calls[-1][1] == "SELECT id, name, email FROM users LIMIT $1 OFFSET $2"


async def test_get_uses_identity_map_and_skips_second_query() -> None:
    conn = FakeConnection(row=(1, "A", "a@example.com"))
    identity_map = IdentityMap()
    repo = RawModelRepository(conn, User, identity_map=identity_map)

    first = await repo.get(1)
    calls_after_first = len(conn.calls)
    second = await repo.get(1)

    assert first is second
    assert len(conn.calls) == calls_after_first  # there is no second query


async def test_list_does_not_populate_identity_map() -> None:
    conn = FakeConnection(rows=[(1, "A", "a@example.com")])
    identity_map = IdentityMap()
    repo = RawModelRepository(conn, User, identity_map=identity_map)
    await repo.list()
    assert identity_map.get(User, 1) is None  # list does not put rows into the map


async def test_update_returns_model_via_returning() -> None:
    conn = FakeConnection(row=(1, "B", "a@example.com"))
    repo = RawModelRepository(conn, User)
    updated = await repo.update(1, {"name": "B"})
    assert updated == User(id=1, name="B", email="a@example.com")
    assert "RETURNING id, name, email" in conn.calls[-1][1]


async def test_update_missing_row_returns_none() -> None:
    conn = FakeConnection(row=None)
    repo = RawModelRepository(conn, User)
    assert await repo.update(999, {"name": "B"}) is None


# ── integration part ─────────────────────────────────────────────────────────


class RawUser(Model):
    __table__ = TABLE
    id: int = 0
    name: str = ""
    email: str = ""
    age: int = 0


DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id serial PRIMARY KEY,
    name text NOT NULL,
    email text NOT NULL,
    age integer NOT NULL DEFAULT 0
)
"""


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


@pytest.fixture
async def repo(pool):
    conn = await pool.acquire()
    yield RawModelRepository(conn, RawUser, identity_map=IdentityMap())
    await pool.release(conn)


async def test_integration_save_get_list(repo) -> None:
    saved = await repo.save(RawUser(name="A", email="a@example.com", age=30))
    assert saved.id == 1

    fetched = await repo.get(saved.id)
    assert fetched == saved

    await repo.save(RawUser(name="B", email="b@example.com", age=20))
    adults = await repo.list(RawUser.c.age >= 18)
    assert [user.name for user in adults] == ["A", "B"]

    assert await repo.get(9999) is None


async def test_integration_query_fetch(repo) -> None:
    await repo.save(RawUser(name="keep", email="k@example.com", age=40))
    await repo.save(RawUser(name="drop", email="d@example.com", age=10))

    query = (
        select(RawUser)
        .where(RawUser.c.age > 30)
        .order_by(RawUser.c.name)
        .limit(10)
    )
    rows = await repo.fetch(query)
    assert [row.name for row in rows] == ["keep"]


async def test_integration_or_in_fetch_one_fetch_value(repo) -> None:
    for name, email, age in [("a", "a@x", 10), ("b", "b@x", 20), ("c", "c@x", 30)]:
        await repo.save(RawUser(name=name, email=email, age=age))

    rows = await repo.fetch(select(RawUser).where((RawUser.c.age < 15) | (RawUser.c.age > 25)))
    assert sorted(r.age for r in rows) == [10, 30]

    rows = await repo.fetch(select(RawUser).where(RawUser.c.id.in_([1, 3])))
    assert sorted(r.id for r in rows) == [1, 3]

    one = await repo.fetch_one(select(RawUser).where(RawUser.c.age == 20))
    assert one is not None and one.name == "b"

    total = await repo.fetch_value(select(RawUser).count())
    assert total == 3


async def test_integration_update_returns_fresh_model(repo) -> None:
    saved = await repo.save(RawUser(name="A", email="a@example.com", age=30))
    updated = await repo.update(saved.id, {"name": "A2", "age": 31})
    assert updated == RawUser(id=saved.id, name="A2", email="a@example.com", age=31)
    assert (await repo.get(saved.id)).name == "A2"


async def test_integration_delete(repo) -> None:
    saved = await repo.save(RawUser(name="A", email="a@example.com"))
    await repo.delete(saved.id)
    assert await repo.get(saved.id) is None


async def test_integration_identity_map_dedupes(pool) -> None:
    async with RawUnitOfWork(pool) as uow:
        repo = uow.model(RawUser)
        saved = await repo.save(RawUser(name="A", email="a@example.com"))

        # re-reading with the same repository returns the same object
        first = await repo.get(saved.id)
        second = await repo.get(saved.id)
        assert first is second

        # a new uow.model(...) call shares the same identity map
        other = uow.model(RawUser)
        assert await other.get(saved.id) is first


async def test_selects_only_model_columns(pool) -> None:
    """A model may be narrower than its table: SELECT/RETURNING use only its fields."""

    class Partial(Model):
        __table__ = TABLE  # raw_repo_test also has an age column absent from the model
        id: int = 0
        name: str = ""
        email: str = ""

    conn = await pool.acquire()
    try:
        repo = RawModelRepository(conn, Partial)
        saved = await repo.save(Partial(name="P", email="p@example.com"))
        assert saved == Partial(id=1, name="P", email="p@example.com")
        assert await repo.get(1) == Partial(id=1, name="P", email="p@example.com")
        assert await repo.list() == [Partial(id=1, name="P", email="p@example.com")]
    finally:
        await pool.release(conn)


async def test_integration_unit_of_work_commits_and_rolls_back(pool) -> None:
    async with RawUnitOfWork(pool) as uow:
        await uow.model(RawUser).save(RawUser(name="keep", email="k@example.com"))

    with pytest.raises(RuntimeError):
        async with RawUnitOfWork(pool) as uow:
            await uow.model(RawUser).save(RawUser(name="drop", email="d@example.com"))
            raise RuntimeError("boom")

    async with RawUnitOfWork(pool) as uow:
        names = [user.name for user in await uow.model(RawUser).list()]
    assert names == ["keep"]


# ── relations ────────────────────────────────────────────────────────────────

class RelItem(Model):
    __table__ = "rel_items"
    id: int = 0
    parent_id: int = 0
    name: str = ""


class RelParent(Model):
    __table__ = "rel_parents"
    id: int = 0
    items: Annotated[list[RelItem], Relation("parent_id")] = []


async def test_relations_always_loaded(pool) -> None:
    """A declared relation is always loaded: get() and list() fetch children."""
    async with RawUnitOfWork(pool) as uow:
        await uow.connection.execute("DROP TABLE IF EXISTS rel_items, rel_parents")
        await uow.connection.execute("CREATE TABLE rel_parents (id serial PRIMARY KEY)")
        await uow.connection.execute(
            "CREATE TABLE rel_items (id serial PRIMARY KEY, parent_id int NOT NULL, name text NOT NULL)"
        )
        await uow.connection.execute(
            "INSERT INTO rel_parents (id) VALUES (1), (2)"
        )
        await uow.connection.execute(
            "INSERT INTO rel_items (parent_id, name) VALUES (1, 'a'), (1, 'b'), (2, 'c')"
        )

    async with RawUnitOfWork(pool, readonly=True) as uow:
        parent = await uow.model(RelParent).get(1)
        assert [i.name for i in parent.items] == ["a", "b"]

        parents = await uow.model(RelParent).list()
        assert [i.name for i in parents[0].items] == ["a", "b"]
        assert [i.name for i in parents[1].items] == ["c"]


async def test_relation_point_read_uses_join() -> None:
    """get() (point read) loads the relation with a single LEFT JOIN."""

    class JoinConn:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, tuple]] = []

        async def fetch(self, sql: str, *params):
            self.calls.append(("fetch", sql, params))
            # flat JOIN rows: parent id=1 with two children
            return [(1, 1, 1, "a"), (1, 2, 1, "b")]

    conn = JoinConn()
    repo = RawModelRepository(conn, RelParent)
    parent = await repo.get(1)
    assert len(conn.calls) == 1
    assert "LEFT JOIN" in conn.calls[0][1]
    assert parent is not None
    assert [i.name for i in parent.items] == ["a", "b"]


async def test_relation_list_uses_separate_query() -> None:
    """list() (bulk read) loads the relation with a separate query, not a JOIN."""

    class ListConn:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, tuple]] = []

        async def fetch(self, sql: str, *params):
            self.calls.append(("fetch", sql, params))
            if "rel_items" in sql:
                # separate child query: id, parent_id, name
                return [(1, 1, "a"), (2, 1, "b")]
            return [(1,)]  # parent id=1

    conn = ListConn()
    repo = RawModelRepository(conn, RelParent)
    parents = await repo.list()
    assert len(conn.calls) == 2
    assert "LEFT JOIN" not in conn.calls[0][1]
    assert "ANY" in conn.calls[1][1]
    assert [p.id for p in parents] == [1]
    assert [i.name for i in parents[0].items] == ["a", "b"]


# ── explicit join ────────────────────────────────────────────────────────────


class JoinParent(Model):
    __table__ = "join_parents"
    id: int = 0
    title: str = ""


class JoinChild(Model):
    __table__ = "join_children"
    id: int = 0
    parent_id: int = 0
    name: str = ""


def test_join_fk_compiles_left_join() -> None:
    sql, params = select(JoinParent).join(JoinChild, fk="parent_id").compile()
    assert sql == (
        "SELECT t0.id, t0.title, t1.id, t1.parent_id, t1.name "
        "FROM (SELECT id, title FROM join_parents) t0 "
        "LEFT JOIN join_children t1 ON t1.parent_id = t0.id"
    )
    assert params == []


def test_join_on_pair_compiles() -> None:
    q = select(JoinParent).join(JoinChild, on=JoinParent.c.id == JoinChild.c.parent_id)
    sql, params = q.compile()
    assert "LEFT JOIN join_children t1 ON t1.parent_id = t0.id" in sql
    assert params == []


def test_join_kind_inner_compiles() -> None:
    sql_text, params = select(JoinParent).join(JoinChild, fk="parent_id", kind=sql.INNER).compile()
    assert sql_text == (
        "SELECT t0.id, t0.title, t1.id, t1.parent_id, t1.name "
        "FROM (SELECT id, title FROM join_parents) t0 "
        "INNER JOIN join_children t1 ON t1.parent_id = t0.id"
    )
    assert params == []


def test_join_kind_right_and_full_compile() -> None:
    for kind in (sql.RIGHT, sql.FULL):
        sql_text, _params = select(JoinParent).join(JoinChild, fk="parent_id", kind=kind).compile()
        assert f"{kind} JOIN join_children t1 ON t1.parent_id = t0.id" in sql_text


def test_join_kind_full_is_refused_on_mysql() -> None:
    """MySQL has no FULL OUTER JOIN — refuse instead of SQL the server would reject."""
    query = select(JoinParent).join(JoinChild, fk="parent_id", kind=sql.FULL)
    with pytest.raises(sql.DialectError):
        query.compile(sql.MYSQL)


def test_unknown_join_kind_is_refused() -> None:
    query = select(JoinParent).join(JoinChild, fk="parent_id", kind="NATURAL")
    with pytest.raises(ValueError):
        query.compile()


async def test_fetch_joined_returns_pairs() -> None:
    class Conn:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple]] = []

        async def fetch(self, sql: str, *params):
            self.calls.append((sql, params))
            return [
                (1, "p1", 1, 1, "a"),
                (1, "p1", 2, 1, "b"),
                (2, "p2", 3, 2, "c"),
            ]

    conn = Conn()
    repo = RawModelRepository(conn, JoinParent)
    rows = await repo.fetch_joined(select(JoinParent).join(JoinChild, fk="parent_id"))
    assert len(rows) == 2

    parent1, children1 = rows[0]
    assert parent1 == JoinParent(id=1, title="p1")
    assert [c.name for c in children1] == ["a", "b"]

    parent2, children2 = rows[1]
    assert parent2 == JoinParent(id=2, title="p2")
    assert [c.name for c in children2] == ["c"]


# ── base repository ──────────────────────────────────────────────────────────

UsersPool = Annotated[Pool, "users"]


class UserAgg:
    """Domain object (not a row model)."""

    def __init__(self, id: int, name: str, email: str):
        self.id = id
        self.name = name
        self.email = email


class UserMapper(Mapper[UserAgg, User]):
    def to_aggregate(self, m: User) -> UserAgg:
        return UserAgg(m.id, m.name, m.email)

    def from_aggregate(self, a: UserAgg) -> User:
        return User(id=a.id, name=a.name, email=a.email)


class UserRepo(Repository[UserAgg, User, UserMapper, UsersPool]):
    pass


def test_repository_extracts_generic_params() -> None:
    assert UserRepo.aggregate is UserAgg
    assert UserRepo.model is User
    assert UserRepo.mapper_cls is UserMapper
    assert UserRepo.pool is UsersPool


def test_repository_di_resolves_pool_from_generic() -> None:
    from ferronit.di import Container

    dummy = object()
    c = Container()
    c.singleton(UsersPool, dummy)
    c.factory(UserRepo)

    repo = c.get(UserRepo)
    assert repo._pool is dummy


async def test_repository_crud_roundtrip() -> None:
    try:
        pool = await create_raw_pool(DSN, min_size=1, max_size=4)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"PostgreSQL is unavailable: {type(exc).__name__}")

    wrapped = Pool(pool)
    try:
        async with RawUnitOfWork(pool) as uow:
            await uow.connection.execute("DROP TABLE IF EXISTS users")
            await uow.connection.execute(
                "CREATE TABLE users (id int PRIMARY KEY, name text NOT NULL, email text NOT NULL)"
            )
        repo = UserRepo(wrapped)
        await repo.save(UserAgg(id=1, name="A", email="a@x.io"))

        fetched = await repo.get(1)
        assert isinstance(fetched, UserAgg)
        assert fetched.name == "A"

        # a repeated save with the same id is a replacement (upsert), not an insert
        await repo.save(UserAgg(id=1, name="B", email="a@x.io"))
        assert (await repo.get(1)).name == "B"

        assert len(await repo.list()) == 1

        await repo.delete(1)
        assert await repo.get(1) is None
    finally:
        await pool.close()


class AggItem(Model):
    __table__ = "agg_items"
    id: int = 0
    parent_id: int = 0
    name: str = ""


class AggParent(Model):
    __table__ = "agg_parents"
    id: int = 0
    title: str = ""
    items: Annotated[list[AggItem], Relation("parent_id")] = []


class ParentAgg:
    def __init__(self, id: int, title: str, names: list[str]):
        self.id = id
        self.title = title
        self.names = names


class ParentMapper(Mapper[ParentAgg, AggParent]):
    def to_aggregate(self, m: AggParent) -> ParentAgg:
        return ParentAgg(m.id, m.title, [i.name for i in m.items])

    def from_aggregate(self, a: ParentAgg) -> AggParent:
        return AggParent(
            id=a.id, title=a.title,
            items=[AggItem(parent_id=a.id, name=n) for n in a.names],
        )


class ParentRepo(Repository[ParentAgg, AggParent, ParentMapper, UsersPool]):
    pass


async def test_repository_save_loads_aggregate_with_relations() -> None:
    try:
        pool = await create_raw_pool(DSN, min_size=1, max_size=4)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"PostgreSQL is unavailable: {type(exc).__name__}")

    wrapped = Pool(pool)
    try:
        async with RawUnitOfWork(pool) as uow:
            await uow.connection.execute("DROP TABLE IF EXISTS agg_items, agg_parents")
            await uow.connection.execute(
                "CREATE TABLE agg_parents (id int PRIMARY KEY, title text NOT NULL)"
            )
            await uow.connection.execute(
                "CREATE TABLE agg_items (id serial PRIMARY KEY, parent_id int NOT NULL, name text NOT NULL)"
            )
        repo = ParentRepo(wrapped)
        await repo.save(ParentAgg(id=1, title="t", names=["a", "b"]))

        agg = await repo.get(1)
        assert isinstance(agg, ParentAgg)
        assert agg.names == ["a", "b"]

        # a save with the same id replaces both the header and the child rows
        await repo.save(ParentAgg(id=1, title="t2", names=["c"]))
        agg = await repo.get(1)
        assert agg is not None
        assert agg.title == "t2"
        assert agg.names == ["c"]

        await repo.delete(1)
        assert await repo.get(1) is None
        async with RawUnitOfWork(pool) as uow:
            n = await uow.connection.fetchval("SELECT count(*) FROM agg_items")
        assert n == 0
    finally:
        await pool.close()
