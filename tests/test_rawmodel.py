"""Тесты декларативных моделей над сырым asyncpg-слоем (``ferrox.contrib.rawmodel``).

Юнит-часть (плюрализация имён таблиц, компиляция ``Query``, ``IdentityMap``,
репозиторий с фейковым соединением) работает без базы. Интеграционная часть
требует контейнер PostgreSQL на :5432 и пропускается, если его нет:

    docker run -d --name ferrox-pg -e POSTGRES_PASSWORD=postgres \\
        -e POSTGRES_USER=postgres -e POSTGRES_DB=postgres -p 5432:5432 postgres:18
"""

from __future__ import annotations

import pytest

from ferrox.contrib.rawdb import RawUnitOfWork, create_raw_pool
from ferrox.contrib.rawmodel import (
    IdentityMap,
    Model,
    RawModelRepository,
    select,
)

try:  # asyncpg живёт в extra "postgres"
    import asyncpg
except ModuleNotFoundError:  # pragma: no cover - окружение без extra
    asyncpg = None  # type: ignore[assignment]

DSN = "postgresql://postgres:postgres@localhost:5432/postgres"
TABLE = "raw_model_test"

pytestmark = pytest.mark.skipif(asyncpg is None, reason="asyncpg не установлен (extra postgres)")


# ── модели для юнит-тестов ───────────────────────────────────────────────────


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


# ── имя таблицы: плюрализация ────────────────────────────────────────────────


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


# ── компиляция Query ─────────────────────────────────────────────────────────


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


# ── модель как датакласс ─────────────────────────────────────────────────────


def test_columns_capture_declaration_order() -> None:
    assert User.__columns__ == ("id", "name", "email")


def test_from_row_and_to_tuple_roundtrip() -> None:
    user = User.from_row((1, "A", "a@example.com"))
    assert user == User(id=1, name="A", email="a@example.com")
    assert user.to_tuple() == (1, "A", "a@example.com")


def test_identity_map_holds_strong_references() -> None:
    """Кэш живёт вместе с unit of work и держит строки, пока его не выбросят."""
    identity_map = IdentityMap()
    user = User.from_row((1, "A", "a@example.com"))
    identity_map.put(User, 1, user)
    del user
    assert identity_map.get(User, 1) == User(id=1, name="A", email="a@example.com")


def test_model_instance_is_unhashable() -> None:
    with pytest.raises(TypeError):
        hash(User(id=1, name="A", email="a@example.com"))


# ── репозиторий на фейковом соединении ───────────────────────────────────────


class FakeConnection:
    """Минимальная заглушка asyncpg-соединения, записывающая вызовы."""

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
    assert conn.calls[-1][2] == ("Zoe", "z@example.com")  # pk не уходит в INSERT


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
    assert len(conn.calls) == calls_after_first  # второго запроса нет


async def test_list_does_not_populate_identity_map() -> None:
    conn = FakeConnection(rows=[(1, "A", "a@example.com")])
    identity_map = IdentityMap()
    repo = RawModelRepository(conn, User, identity_map=identity_map)
    await repo.list()
    assert identity_map.get(User, 1) is None  # list не кладёт строки в карту


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


# ── интеграционная часть ─────────────────────────────────────────────────────


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

        # повторное чтение тем же репозиторием вернёт тот же объект
        first = await repo.get(saved.id)
        second = await repo.get(saved.id)
        assert first is second

        # новый вызов uow.model(...) делит ту же identity map
        other = uow.model(RawUser)
        assert await other.get(saved.id) is first


async def test_selects_only_model_columns(pool) -> None:
    """Модель может быть уже таблицы: SELECT/RETURNING берут только её поля."""

    class Partial(Model):
        __table__ = TABLE  # в raw_repo_test есть ещё колонка age, которой нет в модели
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
