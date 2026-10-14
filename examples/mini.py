"""Ferronit — минимальный сервис: агрегат + репозиторий + маршруты (без порта, без CQRS).

Самое простое, что есть в Ferronit для сервиса с БД: хендлеры берут репозиторий
из DI напрямую — ни входящего порта, ни сервиса, ни шин, ни DTO. Порт/адаптер
появляются, когда сервис растёт (см. ``examples/app.py``).

    uvicorn examples.mini:app
"""

import os
from typing import Annotated

import msgspec

from ferronit import Body, Ferronit
from ferronit.contrib.rawdb import Pool, create_pool
from ferronit.contrib.rawmodel import Mapper, Model, Repository
from ferronit.ddd import AggregateRoot
from ferronit.di import Container

DB_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/postgres")
ShopPool = Annotated[Pool, "shop"]

container = Container()
app = Ferronit(debug=True, container=container)


# ── Домен ─────────────────────────────────────────────────────────────

class Order(AggregateRoot):
    def __init__(self, order_id: str, user_id: str, amount: int):
        super().__init__()
        self.id = order_id
        self.user_id = user_id
        self.amount = amount
        self.status = "created"

    def cancel(self) -> None:
        self.status = "cancelled"


# ── Персистентность (модель + маппер + репозиторий) ───────────────────

class OrderModel(Model):
    __table__ = "mini_orders"
    id: str = ""
    user_id: str = ""
    amount: int = 0
    status: str = "created"


class OrderMapper(Mapper[Order, OrderModel]):
    def to_aggregate(self, m: OrderModel) -> Order:
        o = Order(m.id, m.user_id, m.amount)
        o.status = m.status
        return o

    def from_aggregate(self, o: Order) -> OrderModel:
        return OrderModel(id=o.id, user_id=o.user_id, amount=o.amount, status=o.status)


class OrderRepository(Repository[Order, OrderModel, OrderMapper, ShopPool]):
    pass


class CreateOrderBody(msgspec.Struct):
    order_id: str
    user_id: str = "user-1"
    amount: int = 0


# ── DI + жизненный цикл ───────────────────────────────────────────────

container.factory(OrderRepository)  # до объявления маршрутов (DI по аннотации)


@app.lifespan
async def lifespan():
    pool = await create_pool(DB_URL, min_size=1, max_size=16)
    container.singleton(ShopPool, pool)
    async with pool.uow() as uow:
        await uow.connection.execute(
            "CREATE TABLE IF NOT EXISTS mini_orders ("
            "id text PRIMARY KEY, user_id text NOT NULL, amount integer NOT NULL, "
            "status text NOT NULL DEFAULT 'created')"
        )
    yield
    await pool.close()


# ── Маршруты: репозиторий из DI напрямую ──────────────────────────────

@app.route("/orders", methods=["POST"], summary="Create order")
async def create_order(body: Annotated[CreateOrderBody, Body()], orders: OrderRepository):
    order = Order(body.order_id, body.user_id, body.amount)
    await orders.save(order)
    return {"order_id": order.id, "status": order.status}


@app.route("/orders/{order_id}", summary="Get order")
async def get_order(order_id: str, orders: OrderRepository):
    order = await orders.get(order_id)
    if order is None:
        return {"error": "not found"}, 404
    return {"order_id": order.id, "user_id": order.user_id,
            "amount": order.amount, "status": order.status}


@app.route("/orders/{order_id}/cancel", methods=["POST"], summary="Cancel order")
async def cancel_order(order_id: str, orders: OrderRepository):
    order = await orders.get(order_id)
    if order is None:
        return {"error": "not found"}, 404
    order.cancel()
    await orders.save(order)
    return {"order_id": order.id, "status": order.status}


@app.route("/ping")
async def ping():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)
