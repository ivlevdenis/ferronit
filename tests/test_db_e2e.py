"""E2E: Ferrox HTTP → handler → database (SQLite) → response.

Полный путь запроса: HTTP-запрос через ASGI → хендлер пишет/читает
через RelationalUnitOfWork → ответ. Плюс raw SQL проверки.
"""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Column, Integer, String, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ferrox import Ferrox
from ferrox.contrib.db import Base, RelationalUnitOfWork


class OrderModel(Base):
    __tablename__ = "orders"
    id = Column(String, primary_key=True)
    user_id = Column(String, nullable=False)
    amount = Column(Integer, nullable=False)
    status = Column(String, default="created")


@pytest.fixture
async def uow():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield RelationalUnitOfWork(factory)
    await engine.dispose()


def make_app(uow: RelationalUnitOfWork) -> Ferrox:
    app = Ferrox()

    @app.route("/orders", methods=["POST"])
    async def create_order(req):
        body = await req.json()
        async with uow:
            repo = uow[OrderModel]
            order = OrderModel(
                id=body["id"],
                user_id=body.get("user_id", "anonymous"),
                amount=body["amount"],
            )
            await repo.save(order)
        return {"order_id": order.id, "status": order.status}

    @app.route("/orders/{order_id}")
    async def get_order(req):
        async with uow:
            repo = uow[OrderModel]
            order = await repo.get(req.params["order_id"])
        if order is None:
            return {"error": "not found"}
        return {
            "order_id": order.id,
            "user_id": order.user_id,
            "amount": order.amount,
            "status": order.status,
        }

    @app.route("/orders")
    async def list_orders(req):
        async with uow:
            repo = uow[OrderModel]
            orders = await repo.list()
        return {
            "orders": [
                {"id": o.id, "amount": o.amount, "status": o.status} for o in orders
            ]
        }

    return app


@pytest.fixture
async def client(uow):
    app = make_app(uow)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_create_and_get_order(client, uow):
    # POST — запись в БД через UnitOfWork
    r = await client.post("/orders", json={"id": "o1", "user_id": "denis", "amount": 1500})
    assert r.status_code == 200
    assert r.json() == {"order_id": "o1", "status": "created"}

    # GET — чтение из БД (новая сессия, данные реально закоммичены)
    r = await client.get("/orders/o1")
    assert r.status_code == 200
    assert r.json() == {
        "order_id": "o1",
        "user_id": "denis",
        "amount": 1500,
        "status": "created",
    }


@pytest.mark.asyncio
async def test_get_missing_order(client):
    r = await client.get("/orders/nonexistent")
    assert r.status_code == 200
    assert r.json() == {"error": "not found"}


@pytest.mark.asyncio
async def test_list_orders(client):
    await client.post("/orders", json={"id": "a", "amount": 100})
    await client.post("/orders", json={"id": "b", "user_id": "u2", "amount": 200})

    r = await client.get("/orders")
    assert r.status_code == 200
    assert r.json() == {
        "orders": [
            {"id": "a", "amount": 100, "status": "created"},
            {"id": "b", "amount": 200, "status": "created"},
        ]
    }


@pytest.mark.asyncio
async def test_raw_sql_query(client, uow):
    """Настоящий SQL-запрос напрямую к БД после HTTP-записи."""
    await client.post("/orders", json={"id": "x", "amount": 42})

    async with uow:
        session = uow.session
        result = await session.execute(text("SELECT count(*) FROM orders"))
        count = result.scalar_one()
        result = await session.execute(
            text("SELECT amount FROM orders WHERE id = :oid"), {"oid": "x"}
        )
        amount = result.scalar_one()

    assert count == 1
    assert amount == 42


@pytest.mark.asyncio
async def test_rollback_on_error(client, uow):
    """Ошибка в хендлере → транзакция откатывается, в БД пусто."""

    app = make_app(uow)

    @app.route("/boom")
    async def boom(req):
        async with uow:
            repo = uow[OrderModel]
            await repo.save(OrderModel(id="ghost", user_id="x", amount=1))
            raise RuntimeError("boom")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/boom")
        assert r.status_code == 500

    async with uow:
        result = await uow.session.execute(text("SELECT count(*) FROM orders"))
        assert result.scalar_one() == 0
