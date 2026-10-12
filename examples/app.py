"""Ferronit — гексагональный пример: агрегат Order + Mapper + Repository.

Заказ — агрегат (header + позиции), хранится в двух таблицах, но сервис и
маршруты работают только с доменным ``Order``. Маппинг сосредоточен в одном
адаптере ``OrderMapper``; ``OrderRepository`` — порт над агрегатом.

Endpoints:
  POST /orders              — создать заказ (агрегат + позиции)
  GET  /orders              — список заказов
  GET  /orders/{id}         — заказ со всеми позициями
  POST /orders/{id}/cancel  — отменить заказ (UPDATE статуса)
  GET  /openapi.json
  GET  /ping
"""

import asyncio
import os
from typing import Annotated

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

import msgspec

from ferronit import Ferronit
from ferronit.contrib.rawdb import Pool, create_pool
from ferronit.contrib.rawmodel import Mapper, Model, Relation, Repository
from ferronit.ddd import AggregateRoot
from ferronit.di import Container

DB_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/postgres")

ShopPool = Annotated[Pool, "shop"]

container = Container()
app = Ferronit(debug=True, container=container)


# ── Домен (без SQL) ────────────────────────────────────────────────────

class CartItem:
    def __init__(self, product_id: str, name: str, price: float, qty: int):
        self.product_id = product_id
        self.name = name
        self.price = price
        self.qty = qty


class Order(AggregateRoot):
    def __init__(self, order_id: str, user_id: str, items: list[CartItem]):
        super().__init__()
        self.id = order_id
        self.user_id = user_id
        self.items = items
        self.total = sum(i.price * i.qty for i in items)
        self.status = "created"

    def cancel(self):
        if self.status == "shipped":
            raise ValueError("Cannot cancel shipped order")
        self.status = "cancelled"


# ── Персистентность (rawmodel) — чистые модели строк ───────────────────

class OrderItemModel(Model):
    __table__ = "order_items"
    id: int = 0
    order_id: str = ""
    product_id: str = ""
    name: str = ""
    price: float = 0.0
    qty: int = 0


class OrderModel(Model):
    __table__ = "orders"
    id: str = ""
    user_id: str = ""
    total: float = 0.0
    status: str = "created"
    items: Annotated[list[OrderItemModel], Relation("order_id")] = []


# ── Адаптер: маппер (единственное место, где домен встречается с БД) ───

class OrderMapper(Mapper[Order, OrderModel]):
    def to_aggregate(self, m: OrderModel) -> Order:
        return Order(
            m.id, m.user_id,
            [CartItem(i.product_id, i.name, i.price, i.qty) for i in m.items],
        )

    def from_aggregate(self, o: Order) -> OrderModel:
        return OrderModel(
            id=o.id, user_id=o.user_id, total=o.total, status=o.status,
            items=[OrderItemModel(order_id=o.id, product_id=i.product_id,
                                  name=i.name, price=i.price, qty=i.qty)
                   for i in o.items],
        )


# ── Репозиторий (порт над агрегатом, не над таблицей) ─────────────────

class OrderRepository(Repository[Order, OrderModel, OrderMapper, ShopPool]):
    async def by_user(self, user_id: str) -> list[Order]:
        return await self.list(OrderModel.c.user_id == user_id)


# ── Application service ────────────────────────────────────────────────

class OrderService:
    def __init__(self, orders: OrderRepository):
        self._orders = orders

    async def place_order(self, user_id: str, items: list[CartItem]) -> Order | None:
        if not items:
            return None
        order = Order(f"ord-{user_id}-{len(items)}", user_id, items)
        await self._orders.save(order)          # агрегат + позиции в одной UoW
        return order

    async def get_order(self, order_id: str) -> Order | None:
        return await self._orders.get(order_id)

    async def cancel(self, order_id: str) -> Order | None:
        order = await self._orders.get(order_id)
        if order is None:
            return None
        order.cancel()
        await self._orders.save(order)          # замена агрегата целиком
        return order


# ── DI wiring ──────────────────────────────────────────────────────────

container.factory(OrderRepository)
container.factory(OrderService)


# ── Lifecycle ──────────────────────────────────────────────────────────

@app.lifespan
async def lifespan():
    pool = await create_pool(DB_URL, min_size=1, max_size=16)
    container.singleton(ShopPool, pool)
    async with pool.uow() as uow:
        await uow.connection.execute(
            "CREATE TABLE IF NOT EXISTS orders ("
            "id text PRIMARY KEY, user_id text NOT NULL, total double precision NOT NULL, "
            "status text NOT NULL DEFAULT 'created')"
        )
        await uow.connection.execute(
            "CREATE TABLE IF NOT EXISTS order_items ("
            "id serial PRIMARY KEY, order_id text NOT NULL REFERENCES orders(id), "
            "product_id text NOT NULL, name text NOT NULL, "
            "price double precision NOT NULL, qty integer NOT NULL)"
        )
    yield
    await pool.close()


# ── Response model ─────────────────────────────────────────────────────

class OrderResponse(msgspec.Struct):
    order_id: str
    user_id: str
    items: list[dict]
    total: float
    status: str


def _to_response(o: Order) -> OrderResponse:
    return OrderResponse(
        order_id=o.id,
        user_id=o.user_id,
        items=[{"product_id": i.product_id, "name": i.name,
                "price": i.price, "qty": i.qty} for i in o.items],
        total=o.total,
        status=o.status,
    )


# ── Routes ─────────────────────────────────────────────────────────────

@app.route("/orders", methods=["POST"], summary="Create order")
async def create_order(req, orders: OrderService):
    body = await req.json()
    items = [CartItem(i["product_id"], i.get("name", "Unknown"),
                      i.get("price", 0), i.get("qty", 1)) for i in body["items"]]
    order = await orders.place_order(body.get("user_id", "user-1"), items)
    if order is None:
        return {"error": "items required"}, 400
    return _to_response(order)


@app.route("/orders", summary="List orders")
async def list_orders(req, orders: OrderRepository) -> list[OrderResponse]:
    return [_to_response(o) for o in await orders.by_user("user-1")]


@app.route("/orders/{order_id}", summary="Get order")
async def get_order(req, orders: OrderService):
    order = await orders.get_order(req.params["order_id"])
    if order is None:
        return {"error": "not found"}, 404
    return _to_response(order)


@app.route("/orders/{order_id}/cancel", methods=["POST"], summary="Cancel order")
async def cancel_order(req, orders: OrderService):
    order = await orders.cancel(req.params["order_id"])
    if order is None:
        return {"error": "not found"}, 404
    return _to_response(order)


@app.route("/openapi.json")
def openapi_spec(req):
    return app.openapi.build()


@app.route("/ping")
async def ping():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, port=8000)
