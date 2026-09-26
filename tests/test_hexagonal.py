"""Hexagonal architecture integration tests."""

import pytest
from httpx import ASGITransport, AsyncClient

from velox import Velox
from velox.ddd import AggregateRoot, Command, CommandBus
from velox.hexagonal import Adapter, ApplicationService, Port, UnitOfWork


# ── Domain ─────────────────────────────────────────────────────────────

class Order(AggregateRoot):
    def __init__(self, order_id: str, amount: int):
        super().__init__()
        self.id = order_id
        self.amount = amount


# ── Ports ─────────────────────────────────────────────────────────────

class OrderRepository(Port):
    async def get(self, id: str) -> Order | None: ...
    async def save(self, order: Order) -> None: ...


class PaymentGateway(Port):
    async def charge(self, amount: int) -> bool: ...


# ── Adapters ──────────────────────────────────────────────────────────

class InMemoryOrderRepo(OrderRepository):
    def __init__(self):
        self._store: dict[str, Order] = {}

    async def get(self, id: str) -> Order | None:
        return self._store.get(id)

    async def save(self, order: Order) -> None:
        self._store[order.id] = order


class FakePaymentGateway(PaymentGateway):
    async def charge(self, amount: int) -> bool:
        return True


class InMemoryUoW(UnitOfWork):
    async def commit(self): pass
    async def rollback(self): pass


# ── Application ───────────────────────────────────────────────────────

class CreateOrderCmd(Command):
    __slots__ = ("order_id", "amount")
    def __init__(self, oid: str, amount: int):
        self.order_id = oid
        self.amount = amount


class OrderService(ApplicationService):
    def __init__(self, repo: OrderRepository, payment: PaymentGateway, uow: UnitOfWork):
        super().__init__(uow)
        self._repo = repo
        self._payment = payment

    async def create(self, cmd: CreateOrderCmd) -> str:
        async with self.uow.transaction():
            paid = await self._payment.charge(cmd.amount)
            if not paid:
                raise Exception("Payment failed")
            order = Order(cmd.order_id, cmd.amount)
            await self._repo.save(order)
        return order.id


# ── Tests ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hexagonal_full():
    repo = InMemoryOrderRepo()
    payment = FakePaymentGateway()
    uow = InMemoryUoW()
    service = OrderService(repo, payment, uow)

    bus = CommandBus()
    bus.register(CreateOrderCmd, service.create)

    v = Velox()

    @v.route("/orders", methods=["POST"])
    async def create_order(req):
        body = await req.json()
        oid = await bus.dispatch(CreateOrderCmd(body["id"], body["amount"]))
        return {"order_id": oid}

    transport = ASGITransport(app=v)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/orders", json={"id": "ord-1", "amount": 100})
        assert r.json() == {"order_id": "ord-1"}
