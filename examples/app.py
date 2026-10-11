"""Ferronit E-Commerce API — full DDD + Hexagonal + Postgres (rawmodel + DI).

Endpoints:
  POST /cart/items      — add item to cart
  GET  /cart            — view cart
  POST /orders          — place order from cart
  GET  /orders/{id}     — view order
  GET  /openapi.json    — API spec
  GET  /static/*        — static files
"""

import asyncio
import os
from typing import Annotated

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

from pydantic import BaseModel

from ferronit import Ferronit
from ferronit.contrib.cors import cors
from ferronit.contrib.pydantic.pydantic_codec import install
from ferronit.contrib.rawdb import Pool, create_pool
from ferronit.contrib.rawmodel import Model
from ferronit.contrib.staticfiles import StaticFiles
from ferronit.ddd import AggregateRoot, Command, Query
from ferronit.di import Container

install()

DB_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/postgres")

# Единый пул магазина — маркер различает несколько пулов одного типа.
ShopPool = Annotated[Pool, "shop"]

container = Container()
app = Ferronit(debug=True, container=container)
app.use(cors())
app.mount("/static", StaticFiles("./public"))


# ── Domain Models ──────────────────────────────────────────────────────

class Cart(AggregateRoot):
    def __init__(self, cart_id: str, user_id: str):
        super().__init__()
        self.id = cart_id
        self.user_id = user_id
        self.items: list[CartItem] = []

    def add(self, product_id: str, name: str, price: float, qty: int = 1):
        self.items.append(CartItem(product_id, name, price, qty))

    @property
    def total(self) -> float:
        return sum(i.price * i.qty for i in self.items)


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


# ── DB Models (rawmodel) ───────────────────────────────────────────────

class CartItemModel(Model):
    __table__ = "cart_items"
    id: int = 0
    cart_id: str = ""
    product_id: str = ""
    name: str = ""
    price: float = 0.0
    qty: int = 0


class OrderModel(Model):
    __table__ = "orders"
    id: str = ""
    user_id: str = ""
    total: float = 0.0


# ── Repositories (порты, зависят от конкретного пула) ──────────────────

class CartRepository:
    def __init__(self, pool: ShopPool):
        self._pool = pool

    async def add(self, item: CartItemModel) -> CartItemModel:
        async with self._pool.uow() as uow:
            return await uow.model(CartItemModel).save(item)

    async def list_by_cart(self, cart_id: str) -> list[CartItemModel]:
        async with self._pool.uow(readonly=True) as uow:
            return await uow.model(CartItemModel).list(CartItemModel.c.cart_id == cart_id)


class OrderRepository:
    def __init__(self, pool: ShopPool):
        self._pool = pool

    async def save(self, order: OrderModel) -> OrderModel:
        async with self._pool.uow() as uow:
            return await uow.model(OrderModel).save(order)

    async def get(self, order_id: str) -> OrderModel | None:
        async with self._pool.uow(readonly=True) as uow:
            return await uow.model(OrderModel).get(order_id)


# ── Commands / Queries ────────────────────────────────────────────────

class AddToCartCmd(Command):
    __slots__ = ("name", "price", "product_id", "qty", "user_id")
    def __init__(self, user_id, product_id, name, price, qty=1):
        self.user_id = user_id
        self.product_id = product_id
        self.name = name
        self.price = price
        self.qty = qty


class GetCartQ(Query):
    __slots__ = ("user_id",)
    def __init__(self, user_id): self.user_id = user_id


class PlaceOrderCmd(Command):
    __slots__ = ("user_id",)
    def __init__(self, user_id): self.user_id = user_id


class GetOrderQ(Query):
    __slots__ = ("order_id",)
    def __init__(self, order_id): self.order_id = order_id


# ── Response Models ───────────────────────────────────────────────────

class CartResponse(BaseModel):
    cart_id: str
    user_id: str
    items: list[dict]
    total: float


class OrderResponse(BaseModel):
    order_id: str
    user_id: str
    items: list[dict]
    total: float


# ── Application Services ──────────────────────────────────────────────

class CartService:
    def __init__(self, carts: CartRepository):
        self._carts = carts

    async def add_item(self, cmd: AddToCartCmd) -> Cart:
        cart = Cart(f"cart-{cmd.user_id}", cmd.user_id)
        cart.add(cmd.product_id, cmd.name, cmd.price, cmd.qty)
        for item in cart.items:
            await self._carts.add(CartItemModel(
                id=0,
                cart_id=cart.id,
                product_id=item.product_id,
                name=item.name,
                price=item.price,
                qty=item.qty,
            ))
        return cart

    async def get_cart(self, q: GetCartQ) -> Cart | None:
        rows = await self._carts.list_by_cart(f"cart-{q.user_id}")
        if not rows:
            return None
        cart = Cart(f"cart-{q.user_id}", q.user_id)
        for row in rows:
            cart.add(row.product_id, row.name, row.price, row.qty)
        return cart


class OrderService:
    def __init__(self, cart_service: CartService, orders: OrderRepository):
        self._cart_service = cart_service
        self._orders = orders

    async def place_order(self, cmd: PlaceOrderCmd) -> Order | None:
        cart = await self._cart_service.get_cart(GetCartQ(cmd.user_id))
        if not cart or not cart.items:
            return None

        order = Order(f"ord-{cmd.user_id}-{len(cart.items)}", cmd.user_id, cart.items)
        await self._orders.save(OrderModel(id=order.id, user_id=order.user_id, total=order.total))
        return order

    async def get_order(self, q: GetOrderQ) -> Order | None:
        row = await self._orders.get(q.order_id)
        if not row:
            return None
        return Order(row.id, row.user_id, [])


# ── DI wiring ──────────────────────────────────────────────────────────

container.factory(CartRepository)
container.factory(OrderRepository)
container.factory(CartService)
container.factory(OrderService)


# ── Lifecycle ──────────────────────────────────────────────────────────

@app.lifespan
async def lifespan():
    pool = await create_pool(DB_URL, min_size=1, max_size=16)
    container.singleton(ShopPool, pool)
    async with pool.uow() as uow:
        await uow.connection.execute(
            "CREATE TABLE IF NOT EXISTS cart_items ("
            "id serial PRIMARY KEY, cart_id text NOT NULL, product_id text NOT NULL, "
            "name text NOT NULL, price double precision NOT NULL, qty integer NOT NULL)"
        )
        await uow.connection.execute(
            "CREATE TABLE IF NOT EXISTS orders ("
            "id text PRIMARY KEY, user_id text NOT NULL, total double precision NOT NULL)"
        )
    yield
    await pool.close()


# ── Middleware ─────────────────────────────────────────────────────────

async def request_logger(req, next_handler):
    import time
    start = time.perf_counter()
    result = next_handler(req)
    if hasattr(result, "__await__"):
        result = await result
    elapsed = (time.perf_counter() - start) * 1000
    print(f"  {req.method} {req.path} → {elapsed:.1f}ms")
    return result

app.use(request_logger)


# ── Routes ─────────────────────────────────────────────────────────────

@app.route("/openapi.json")
def openapi_spec(req):
    return app.openapi.build()


@app.route("/cart", summary="View cart")
async def view_cart(req, carts: CartService) -> CartResponse:
    cart = await carts.get_cart(GetCartQ("user-1"))
    if cart is None:
        return CartResponse(cart_id="", user_id="user-1", items=[], total=0)
    return CartResponse(
        cart_id=cart.id,
        user_id=cart.user_id,
        items=[{"product_id": i.product_id, "name": i.name, "price": i.price, "qty": i.qty} for i in cart.items],
        total=cart.total,
    )


@app.route("/cart/items", methods=["POST"], summary="Add item to cart")
async def add_item(req, carts: CartService) -> CartResponse:
    body = await req.json()
    cmd = AddToCartCmd(
        user_id="user-1",
        product_id=body["product_id"],
        name=body.get("name", "Unknown"),
        price=body.get("price", 0),
        qty=body.get("qty", 1),
    )
    cart = await carts.add_item(cmd)
    return CartResponse(
        cart_id=cart.id,
        user_id=cart.user_id,
        items=[{"product_id": i.product_id, "name": i.name, "price": i.price, "qty": i.qty} for i in cart.items],
        total=cart.total,
    )


@app.route("/orders", methods=["POST"], summary="Place order")
async def place_order(req, orders: OrderService):
    order = await orders.place_order(PlaceOrderCmd("user-1"))
    if order is None:
        return {"error": "Cart is empty"}
    return OrderResponse(
        order_id=order.id,
        user_id=order.user_id,
        items=[{"product_id": i.product_id, "name": i.name, "price": i.price, "qty": i.qty} for i in order.items],
        total=order.total,
    )


@app.route("/orders/{order_id}", summary="Get order")
async def get_order(req, orders: OrderService) -> OrderResponse:
    order = await orders.get_order(GetOrderQ(req.params["order_id"]))
    if order is None:
        return {"error": "not found"}, 404
    return OrderResponse(
        order_id=order.id,
        user_id=order.user_id,
        items=[{"product_id": i.product_id, "name": i.name, "price": i.price, "qty": i.qty} for i in order.items],
        total=order.total,
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, port=8000)
