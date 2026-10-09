"""E2E: Ferronit HTTP → CoreRepository → реальный PostgreSQL (Docker, дефолтные настройки).

Требует: docker run -d --name ferronit-pg -e POSTGRES_PASSWORD=postgres \
    -e POSTGRES_USER=postgres -e POSTGRES_DB=postgres -p 5432:5432 postgres:latest
"""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Column, Integer, MetaData, String, Table, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ferronit import Ferronit
from ferronit.contrib.db import RelationalUnitOfWork

DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/postgres"

metadata = MetaData()

orders_table = Table(
    "orders_core_test",
    metadata,
    Column("id", String, primary_key=True),
    Column("user_id", String, nullable=False),
    Column("amount", Integer, nullable=False),
    Column("status", String, default="created"),
)


@pytest.fixture
async def uow():
    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.drop_all)
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
async def test_pg_create_and_get(client):
    r = await client.post("/orders", json={"id": "pg1", "user_id": "denis", "amount": 1500})
    assert r.status_code == 200
    assert r.json() == {"order_id": "pg1", "status": "created"}

    r = await client.get("/orders/pg1")
    assert r.status_code == 200
    assert r.json() == {"id": "pg1", "user_id": "denis", "amount": 1500, "status": "created"}


@pytest.mark.asyncio
async def test_pg_list(client):
    await client.post("/orders", json={"id": "a", "amount": 100})
    await client.post("/orders", json={"id": "b", "user_id": "u2", "amount": 200})

    r = await client.get("/orders")
    assert r.status_code == 200
    assert len(r.json()["orders"]) == 2


@pytest.mark.asyncio
async def test_pg_raw_sql(client, uow):
    """Настоящий SQL к Postgres после HTTP-записи."""
    await client.post("/orders", json={"id": "x", "amount": 42})

    async with uow:
        result = await uow.session.execute(
            text("SELECT amount FROM orders_core_test WHERE id = :oid"), {"oid": "x"}
        )
        assert result.scalar_one() == 42


@pytest.mark.asyncio
async def test_pg_returning_defaults(uow):
    """RETURNING возвращает server_defaults (на PG INSERT...RETURNING нативный)."""
    async with uow:
        repo = uow[orders_table]
        row = await repo.save({"id": "d1", "user_id": "u", "amount": 7}, returning=True)
        assert row["status"] == "created"
