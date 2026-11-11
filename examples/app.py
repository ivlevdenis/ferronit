"""Ferronit — hexagonal CQRS example: Order aggregate + Mapper + Repository.

This is the **application** layer: domain, ports, service, buses and DI. There is
nothing HTTP-related here — the incoming adapter ``HttpOrdersAdapter`` lives in
``examples/http.py`` and is mounted from the composition root
``examples/main.py``:

    app.mount("/", HttpOrdersAdapter())

An order is an aggregate (header + items) stored in two tables, yet the service
works only with the domain ``Order``. ``OrderRepository`` is a port over the
aggregate; the entry point is described by the incoming port ``OrderApiPort``,
implemented by ``OrderService`` (it dispatches commands/queries into the buses).

CQRS: writes go as commands through ``CommandBus`` (``PlaceOrder``/``CancelOrder``),
reads as queries through ``QueryBus`` (``GetOrder``/``ListUserOrders``); the
``OrderPlaced``/``OrderCancelled`` events are published to ``InMemoryEventBus``.
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
from ferronit.ddd import (
    AggregateRoot,
    Command,
    CommandBus,
    DomainEvent,
    Query,
    QueryBus,
)
from ferronit.di import Container
from ferronit.hexagonal import ApplicationService, EventBus, InMemoryEventBus, Port

DB_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/postgres")

ShopPool = Annotated[Pool, "shop"]

container = Container()
app = Ferronit(debug=True, container=container)


# ── Domain (no SQL) ────────────────────────────────────────────────────

class CartItem(msgspec.Struct):
    product_id: str
    name: str = "Unknown"
    price: float = 0.0
    qty: int = 1


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
        # No events are recorded here: rebuilding the aggregate from the DB
        # (to_aggregate) uses the same constructor and must not emit domain events.

    @classmethod
    def place(cls, order_id: str, user_id: str, items: list[CartItem]) -> "Order":
        """Create an order and record the ``OrderPlaced`` event."""
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


# ── Persistence (rawmodel) — pure row models ──────────────────────────

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


# ── Adapter: mapper (the only place where the domain meets the DB) ────

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


# ── Repository (a port over the aggregate, not over the table) ────────

class OrderRepository(Repository[Order, OrderModel, OrderMapper, ShopPool]):
    async def by_user(self, user_id: str) -> list[Order]:
        return await self.list(OrderModel.c.user_id == user_id)


# ── CQRS: commands (writes) ───────────────────────────────────────────

class PlaceOrder(Command):
    user_id: str
    items: list[CartItem]


class CancelOrder(Command):
    order_id: str


class PlaceOrderHandler:
    def __init__(self, orders: OrderRepository, events: EventBus):
        self._orders = orders
        self._events = events

    async def __call__(self, cmd: PlaceOrder) -> Order | None:
        if not cmd.items:
            return None
        order = Order.place(f"ord-{cmd.user_id}-{len(cmd.items)}", cmd.user_id, cmd.items)
        await self._orders.save(order)          # aggregate + items in a single UoW
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
        await self._orders.save(order)          # replace the whole aggregate
        for event in order.pull_events():
            await self._events.publish(event)
        return order


# ── CQRS: queries (reads) ─────────────────────────────────────────────

class GetOrder(Query):
    order_id: str


class ListUserOrders(Query):
    user_id: str


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


# ── Input/output contract: application-layer DTOs ─────────────────────
# The port and the HTTP adapter talk through these structures. The domain
# (Order, CartItem) never leaves the application layer — the service translates
# domain ↔ DTO.

class OrderItemDTO(msgspec.Struct):
    product_id: str
    name: str = "Unknown"
    price: float = 0.0
    qty: int = 1


class CreateOrderDTO(msgspec.Struct):
    user_id: str = "user-1"
    items: list[OrderItemDTO] = []


class OrderDTO(msgspec.Struct):
    order_id: str
    user_id: str
    items: list[OrderItemDTO]
    total: float
    status: str


def _order_to_dto(o: Order) -> OrderDTO:
    return OrderDTO(
        order_id=o.id,
        user_id=o.user_id,
        items=[OrderItemDTO(i.product_id, i.name, i.price, i.qty) for i in o.items],
        total=o.total,
        status=o.status,
    )


# ── Incoming port + application service ───────────────────────────────

class OrderApiPort(Port):
    """Order incoming port — the application-layer contract in DTO terms.

    Neither domain types (``Order``/``CartItem``) nor HTTP appear here: the adapter
    passes ``CreateOrderDTO`` and receives ``OrderDTO``; the service translates.
    """

    async def create(self, dto: CreateOrderDTO) -> OrderDTO | None: ...
    async def list_user_orders(self, user_id: str) -> list[OrderDTO]: ...
    async def get(self, order_id: str) -> OrderDTO | None: ...
    async def cancel(self, order_id: str) -> OrderDTO | None: ...


class OrderService(ApplicationService, OrderApiPort):
    """Application layer: implements ``OrderApiPort``, dispatches commands/queries."""

    def __init__(self, commands: CommandBus, queries: QueryBus):
        super().__init__()
        self._commands = commands
        self._queries = queries

    async def create(self, dto: CreateOrderDTO) -> OrderDTO | None:
        cart = [CartItem(i.product_id, i.name, i.price, i.qty) for i in dto.items]
        order = await self._commands.dispatch(PlaceOrder(dto.user_id, cart))
        return _order_to_dto(order) if order is not None else None

    async def list_user_orders(self, user_id: str) -> list[OrderDTO]:
        orders = await self._queries.ask(ListUserOrders(user_id))
        return [_order_to_dto(o) for o in orders]

    async def get(self, order_id: str) -> OrderDTO | None:
        order = await self._queries.ask(GetOrder(order_id))
        return _order_to_dto(order) if order is not None else None

    async def cancel(self, order_id: str) -> OrderDTO | None:
        order = await self._commands.dispatch(CancelOrder(order_id))
        return _order_to_dto(order) if order is not None else None


# ── Domain event subscriber (audit journal) ───────────────────────────

AUDIT: list[str] = []


async def audit(event: OrderPlaced | OrderCancelled) -> None:
    """Append the domain event to the audit journal and print the line."""
    line = f"{type(event).__name__}: {event.order_id}"
    AUDIT.append(line)
    print(f"[audit] {line}")


# ── DI wiring ──────────────────────────────────────────────────────────
# DI keys must be registered BEFORE the routes are declared: the parameter
# binding mode is chosen once, when the route is registered, and an unknown
# annotation is treated as a path/query parameter rather than DI. That is why
# the buses and the event bus are created here, while handlers are filled in lifespan.

EVENTS = InMemoryEventBus()
EVENTS.subscribe(OrderPlaced, audit)
EVENTS.subscribe(OrderCancelled, audit)

COMMANDS = CommandBus()
QUERIES = QueryBus()

container.factory(OrderRepository)
container.singleton(EventBus, EVENTS)
container.singleton(CommandBus, COMMANDS)
container.singleton(QueryBus, QUERIES)
# Incoming port → implementation. Registered BEFORE the routes are declared:
# binding recognises a DI parameter by the key's presence in the container at
# route registration time, so ``OrderApiPort`` must be here in advance.
container.factory(OrderApiPort, OrderService)


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

    # Handlers are assembled after the pool: the repository resolves lazily, while
    # the buses themselves sit in the container since import (otherwise the routes
    # would not see them as DI).
    orders = container.get(OrderRepository)

    COMMANDS.register(PlaceOrder, PlaceOrderHandler(orders, EVENTS))
    COMMANDS.register(CancelOrder, CancelOrderHandler(orders, EVENTS))
    QUERIES.register(GetOrder, GetOrderHandler(orders))
    QUERIES.register(ListUserOrders, ListOrdersHandler(orders))

    yield
    await pool.close()


# The HTTP adapter (``HttpOrdersAdapter``) lives in examples/http.py and is
# mounted from the composition root examples/main.py: `app.mount("/", HttpOrdersAdapter())`.

