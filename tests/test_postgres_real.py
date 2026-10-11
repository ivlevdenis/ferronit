"""E2E: Ferronit HTTP → RawUnitOfWork → реальный PostgreSQL (Docker, дефолтные настройки).

Требует: docker run -d --name ferronit-pg -e POSTGRES_PASSWORD=postgres \
    -e POSTGRES_USER=postgres -e POSTGRES_DB=postgres -p 5432:5432 postgres:latest
"""
import pytest
from httpx import ASGITransport, AsyncClient

from ferronit import Ferronit
from ferronit.contrib.rawdb import RawUnitOfWork, create_raw_pool

DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/postgres"
TABLE = "orders_core_test"


@pytest.fixture
async def pool():
    p = await create_raw_pool(DATABASE_URL, min_size=1, max_size=4)
    async with RawUnitOfWork(p) as uow:
        await uow.connection.execute(f"DROP TABLE IF EXISTS {TABLE}")
        await uow.connection.execute(
            f"CREATE TABLE {TABLE} ("
            "id text PRIMARY KEY, user_id text NOT NULL, "
            "amount integer NOT NULL, status text DEFAULT 'created')"
        )
    yield p
    await p.close()


def make_app(pool) -> Ferronit:
    app = Ferronit()

    @app.route("/orders", methods=["POST"])
    async def create_order(req):
        body = await req.json()
        async with RawUnitOfWork(pool) as uow:
            row = await uow[TABLE].save(
                {
                    "id": body["id"],
                    "user_id": body.get("user_id", "anonymous"),
                    "amount": body["amount"],
                },
                returning=True,
            )
        return {"order_id": row["id"], "status": row["status"]}

    @app.route("/orders/{order_id}")
    async def get_order(req):
        async with RawUnitOfWork(pool, readonly=True) as uow:
            row = await uow[TABLE].get(req.params["order_id"])
        if row is None:
            return {"error": "not found"}
        return row

    @app.route("/orders")
    async def list_orders(req):
        async with RawUnitOfWork(pool, readonly=True) as uow:
            rows = await uow[TABLE].list()
        return {"orders": rows}

    return app


@pytest.fixture
async def client(pool):
    app = make_app(pool)
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
async def test_pg_raw_sql(client, pool):
    """Настоящий SQL к Postgres после HTTP-записи."""
    await client.post("/orders", json={"id": "x", "amount": 42})

    async with RawUnitOfWork(pool, readonly=True) as uow:
        value = await uow.connection.fetchval(
            f"SELECT amount FROM {TABLE} WHERE id = $1", "x"
        )
        assert value == 42


@pytest.mark.asyncio
async def test_pg_returning_defaults(pool):
    """RETURNING возвращает server_defaults (на PG INSERT...RETURNING нативный)."""
    async with RawUnitOfWork(pool) as uow:
        row = await uow[TABLE].save({"id": "d1", "user_id": "u", "amount": 7}, returning=True)
        assert row["status"] == "created"
