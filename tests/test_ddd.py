"""DDD integration tests — full command/query/aggregate lifecycle."""

import pytest

from velox import Velox
from velox.ddd import (
    AggregateRoot,
    Command,
    CommandBus,
    DomainEvent,
    Query,
    QueryBus,
    Repository,
)
from httpx import ASGITransport, AsyncClient


# ── Domain ─────────────────────────────────────────────────────────────

class UserCreated(DomainEvent):
    pass


class User(AggregateRoot):
    def __init__(self, user_id: str, name: str):
        super().__init__()
        self.id = user_id
        self.name = name
        self.record(UserCreated())


# ── Commands / Queries ────────────────────────────────────────────────

class CreateUser(Command):
    __slots__ = ("user_id", "name")
    def __init__(self, user_id: str, name: str):
        self.user_id = user_id
        self.name = name


class GetUser(Query):
    __slots__ = ("user_id",)
    def __init__(self, user_id: str):
        self.user_id = user_id


# ── Repository ─────────────────────────────────────────────────────────

class InMemoryUserRepo(Repository):
    def __init__(self):
        self._store: dict[str, User] = {}

    async def get(self, user_id: str) -> User | None:
        return self._store.get(user_id)

    async def save(self, user: User) -> None:
        self._store[user.id] = user


# ── Handlers ──────────────────────────────────────────────────────────

class CreateUserHandler:
    def __init__(self, repo: InMemoryUserRepo):
        self._repo = repo

    async def __call__(self, cmd: CreateUser) -> str:
        user = User(cmd.user_id, cmd.name)
        await self._repo.save(user)
        return user.id


class GetUserHandler:
    def __init__(self, repo: InMemoryUserRepo):
        self._repo = repo

    async def __call__(self, q: GetUser) -> dict | None:
        user = await self._repo.get(q.user_id)
        if user is None:
            return None
        return {"id": user.id, "name": user.name}


# ── Tests ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ddd_full_cycle():
    repo = InMemoryUserRepo()
    cmd_bus = CommandBus()
    query_bus = QueryBus()

    cmd_bus.register(CreateUser, CreateUserHandler(repo))
    query_bus.register(GetUser, GetUserHandler(repo))

    user_id = await cmd_bus.dispatch(CreateUser("1", "Denis"))
    assert user_id == "1"

    result = await query_bus.ask(GetUser("1"))
    assert result == {"id": "1", "name": "Denis"}

    assert await query_bus.ask(GetUser("99")) is None


@pytest.mark.asyncio
async def test_ddd_http_integration():
    repo = InMemoryUserRepo()
    cmd_bus = CommandBus()
    query_bus = QueryBus()
    cmd_bus.register(CreateUser, CreateUserHandler(repo))
    query_bus.register(GetUser, GetUserHandler(repo))

    v = Velox(debug=True)

    @v.route("/users", methods=["POST"])
    async def create(req):
        body = await req.json()
        uid = await cmd_bus.dispatch(CreateUser(body["id"], body["name"]))
        return {"user_id": uid}

    @v.route("/users/{user_id}")
    async def get_user(req):
        user = await query_bus.ask(GetUser(req.params["user_id"]))
        if user is None:
            return {"error": "not found"}, 404
        return user

    transport = ASGITransport(app=v)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/users", json={"id": "1", "name": "Denis"})
        assert r.status_code == 200
        assert r.json() == {"user_id": "1"}

        r = await c.get("/users/1")
        assert r.json() == {"id": "1", "name": "Denis"}
