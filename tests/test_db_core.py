"""E2E: Ferronit HTTP → handler → SQLAlchemy Core repository → SQLite.

Тот же путь, что и test_db_e2e, но через Core: строки → dict,
без ORM-маппинга (как в проде на Core).
"""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Column, Integer, MetaData, String, Table, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ferronit import Ferronit
from ferronit.contrib.db import RelationalUnitOfWork

metadata = MetaData()

orders_table = Table(
    "orders",
    metadata,
    Column("id", String, primary_key=True),
    Column("user_id", String, nullable=False),
    Column("amount", Integer, nullable=False),
    Column("status", String, default="created"),
)


@pytest.fixture
async def uow():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield RelationalUnitOfWork(factory)
    await engine.dispose()


def make_app(uow: RelationalUnitOfWork) -> Ferronit:
    app = Ferronit()

    @app.route("/orders", methods=["POST"])
    async def create_order(req):
        body = await req.json()
        async with uow:
            repo = uow[orders_table]
            row = await repo.save(
                {"id": body["id"], "user_id": body.get("user_id", "anonymous"),
                 "amount": body["amount"]},
                returning=True,
            )
        return {"order_id": row["id"], "status": row["status"]}

    @app.route("/orders/{order_id}")
    async def get_order(req):
        async with uow:
            repo = uow[orders_table]
            row = await repo.get(req.params["order_id"])
        if row is None:
            return {"error": "not found"}
        return row

    @app.route("/orders/{order_id}", methods=["PATCH"])
    async def update_order(req):
        body = await req.json()
        async with uow:
            repo = uow[orders_table]
            row = await repo.update(req.params["order_id"], body)
        return row

    @app.route("/orders/{order_id}", methods=["DELETE"])
    async def delete_order(req):
        async with uow:
            repo = uow[orders_table]
            await repo.delete(req.params["order_id"])
        return {"deleted": req.params["order_id"]}

    @app.route("/orders")
    async def list_orders(req):
        async with uow:
            repo = uow[orders_table]
            rows = await repo.list()
        return {"orders": rows}

    return app


@pytest.fixture
async def client(uow):
    app = make_app(uow)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_create_and_get(client):
    r = await client.post("/orders", json={"id": "o1", "user_id": "denis", "amount": 1500})
    assert r.status_code == 200
    assert r.json() == {"order_id": "o1", "status": "created"}

    r = await client.get("/orders/o1")
    assert r.status_code == 200
    assert r.json() == {"id": "o1", "user_id": "denis", "amount": 1500, "status": "created"}


@pytest.mark.asyncio
async def test_update_and_delete(client):
    await client.post("/orders", json={"id": "o2", "amount": 100})

    r = await client.patch("/orders/o2", json={"status": "shipped", "amount": 250})
    assert r.status_code == 200
    assert r.json() == {"id": "o2", "status": "shipped", "amount": 250}

    r = await client.delete("/orders/o2")
    assert r.json() == {"deleted": "o2"}

    r = await client.get("/orders/o2")
    assert r.json() == {"error": "not found"}


@pytest.mark.asyncio
async def test_list_with_filters(client, uow):
    await client.post("/orders", json={"id": "a", "amount": 100})
    await client.post("/orders", json={"id": "b", "user_id": "u2", "amount": 200})

    async with uow:
        repo = uow[orders_table]
        rows = await repo.list(orders_table.c.user_id == "u2")
    assert rows == [{"id": "b", "user_id": "u2", "amount": 200, "status": "created"}]


@pytest.mark.asyncio
async def test_raw_sql_after_http(client, uow):
    """Настоящий SQL после HTTP-записи через Core."""
    await client.post("/orders", json={"id": "x", "amount": 42})

    async with uow:
        result = await uow.session.execute(text("SELECT amount FROM orders WHERE id = :oid"), {"oid": "x"})
        assert result.scalar_one() == 42
