"""Ferronit — гексагональный пример с CQRS: агрегат Order + Mapper + Repository.

Заказ — агрегат (header + позиции), хранится в двух таблицах, но сервис и
маршруты работают только с доменным ``Order``. Маппинг сосредоточен в одном
адаптере ``OrderMapper``; ``OrderRepository`` — порт над агрегатом.

CQRS: запись идёт командами через ``CommandBus`` (``PlaceOrder``,
``CancelOrder``), чтение — запросами через ``QueryBus`` (``GetOrder``,
``ListUserOrders``). Агрегат буферизует доменные события (``OrderPlaced``,
``OrderCancelled``), обработчик команды забирает их через ``pull_events()`` и
публикует в ``InMemoryEventBus`` — подписчик ``audit`` пишет их в журнал.

Endpoints:
  POST /orders              — создать заказ (команда PlaceOrder)
  GET  /orders              — список заказов (запрос ListUserOrders)
  GET  /orders/{id}         — заказ со всеми позициями (запрос GetOrder)
  POST /orders/{id}/cancel  — отменить заказ (команда CancelOrder)
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

from ferronit import Body, Ferronit
from ferronit.contrib.rawdb import Pool, create_pool
from ferronit.contrib.rawmodel import Mapper, Model, Relation, Repository
from ferronit.ddd import (
    AggregateRoot,
    Command,
    CommandBus,
    DomainEvent,
    Query,
    QueryBus,
)
from ferronit.di import Container
from ferronit.hexagonal import EventBus, InMemoryEventBus

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


class OrderPlaced(DomainEvent):
    __slots__ = ("order_id", "total", "user_id")

    def __init__(self, order_id: str, user_id: str, total: float):
        self.order_id = order_id
        self.user_id = user_id
        self.total = total


class OrderCancelled(DomainEvent):
    __slots__ = ("order_id",)

    def __init__(self, order_id: str):
        self.order_id = order_id


class Order(AggregateRoot):
    def __init__(self, order_id: str, user_id: str, items: list[CartItem]):
        super().__init__()
        self.id = order_id
        self.user_id = user_id
        self.items = items
        self.total = sum(i.price * i.qty for i in items)
        self.status = "created"
        # События здесь не пишутся: восстановление агрегата из БД (to_aggregate)
        # идёт тем же конструктором и не должно порождать доменные события.

    @classmethod
    def place(cls, order_id: str, user_id: str, items: list[CartItem]) -> "Order":
        """Создать заказ и записать событие ``OrderPlaced``."""
        order = cls(order_id, user_id, items)
        order.record(OrderPlaced(order_id, user_id, order.total))
        return order

    def cancel(self):
        if self.status == "shipped":
            raise ValueError("Cannot cancel shipped order")
        if self.status == "cancelled":
            return
        self.status = "cancelled"
        self.record(OrderCancelled(self.id))


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


# ── CQRS: команды (запись) ────────────────────────────────────────────

class PlaceOrder(Command):
    __slots__ = ("items", "user_id")

    def __init__(self, user_id: str, items: list[CartItem]):
        self.user_id = user_id
        self.items = items


class CancelOrder(Command):
    __slots__ = ("order_id",)

    def __init__(self, order_id: str):
        self.order_id = order_id


class PlaceOrderHandler:
    def __init__(self, orders: OrderRepository, events: EventBus):
        self._orders = orders
        self._events = events

    async def __call__(self, cmd: PlaceOrder) -> Order | None:
        if not cmd.items:
            return None
        order = Order.place(f"ord-{cmd.user_id}-{len(cmd.items)}", cmd.user_id, cmd.items)
        await self._orders.save(order)          # агрегат + позиции в одной UoW
        for event in order.pull_events():
            await self._events.publish(event)
        return order


class CancelOrderHandler:
    def __init__(self, orders: OrderRepository, events: EventBus):
        self._orders = orders
        self._events = events

    async def __call__(self, cmd: CancelOrder) -> Order | None:
        order = await self._orders.get(cmd.order_id)
        if order is None:
            return None
        order.cancel()
        await self._orders.save(order)          # замена агрегата целиком
        for event in order.pull_events():
            await self._events.publish(event)
        return order


# ── CQRS: запросы (чтение) ────────────────────────────────────────────

class GetOrder(Query):
    __slots__ = ("order_id",)

    def __init__(self, order_id: str):
        self.order_id = order_id


class ListUserOrders(Query):
    __slots__ = ("user_id",)

    def __init__(self, user_id: str):
        self.user_id = user_id


class GetOrderHandler:
    def __init__(self, orders: OrderRepository):
        self._orders = orders

    async def __call__(self, q: GetOrder) -> Order | None:
        return await self._orders.get(q.order_id)


class ListOrdersHandler:
    def __init__(self, orders: OrderRepository):
        self._orders = orders

    async def __call__(self, q: ListUserOrders) -> list[Order]:
        return await self._orders.by_user(q.user_id)


# ── Подписчик на доменные события (журнал) ────────────────────────────

AUDIT: list[str] = []


async def audit(event: OrderPlaced | OrderCancelled) -> None:
    """Записать доменное событие в журнал и напечатать строку."""
    line = f"{type(event).__name__}: {event.order_id}"
    AUDIT.append(line)
    print(f"[audit] {line}")


# ── DI wiring ──────────────────────────────────────────────────────────
# Ключи DI должны быть зарегистрированы ДО объявления маршрутов: способ
# связывания параметров выбирается один раз при регистрации маршрута, и
# незнакомая аннотация трактуется как path/query-параметр, а не как DI.
# Поэтому шины и шина событий созданы здесь, а хендлеры наполняются в lifespan.

EVENTS = InMemoryEventBus()
EVENTS.subscribe(OrderPlaced, audit)
EVENTS.subscribe(OrderCancelled, audit)

COMMANDS = CommandBus()
QUERIES = QueryBus()

container.factory(OrderRepository)
container.singleton(EventBus, EVENTS)
container.singleton(CommandBus, COMMANDS)
container.singleton(QueryBus, QUERIES)


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

    # Хендлеры собираются после пула: репозиторий резолвится лениво, а сами шины
    # лежат в контейнере с импорта (иначе маршруты не увидят их как DI).
    orders = container.get(OrderRepository)

    COMMANDS.register(PlaceOrder, PlaceOrderHandler(orders, EVENTS))
    COMMANDS.register(CancelOrder, CancelOrderHandler(orders, EVENTS))
    QUERIES.register(GetOrder, GetOrderHandler(orders))
    QUERIES.register(ListUserOrders, ListOrdersHandler(orders))

    yield
    await pool.close()


# ── Модели HTTP: тело запроса и ответ ─────────────────────────────────

class OrderItemIn(msgspec.Struct):
    product_id: str
    name: str = "Unknown"
    price: float = 0.0
    qty: int = 1


class CreateOrderBody(msgspec.Struct):
    user_id: str = "user-1"
    items: list[OrderItemIn] = []


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
async def create_order(body: Annotated[CreateOrderBody, Body()], bus: CommandBus):
    items = [CartItem(i.product_id, i.name, i.price, i.qty) for i in body.items]
    order = await bus.dispatch(PlaceOrder(body.user_id, items))
    if order is None:
        return {"error": "items required"}, 400
    return _to_response(order)


@app.route("/orders", summary="List orders")
async def list_orders(bus: QueryBus) -> list[OrderResponse]:
    return [_to_response(o) for o in await bus.ask(ListUserOrders("user-1"))]


@app.route("/orders/{order_id}", summary="Get order")
async def get_order(order_id: str, bus: QueryBus):
    order = await bus.ask(GetOrder(order_id))
    if order is None:
        return {"error": "not found"}, 404
    return _to_response(order)


@app.route("/orders/{order_id}/cancel", methods=["POST"], summary="Cancel order")
async def cancel_order(order_id: str, bus: CommandBus):
    order = await bus.dispatch(CancelOrder(order_id))
    if order is None:
        return {"error": "not found"}, 404
    return _to_response(order)


@app.route("/openapi.json")
def openapi_spec():
    return app.openapi.build()


@app.route("/ping")
async def ping():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, port=8000)
