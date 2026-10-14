"""Hexagonal architecture integration tests."""

import pytest
from httpx import ASGITransport, AsyncClient

from ferronit import Ferronit
from ferronit.ddd import AggregateRoot, Command, CommandBus
from ferronit.hexagonal import ApplicationService, Port, UnitOfWork

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
    order_id: str
    amount: int


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


# ── Входящий порт (driving port) ──────────────────────────────────────

class OrderApiPort(Port):
    async def create(self, order_id: str, amount: int) -> str: ...


class PortBackedService(ApplicationService, OrderApiPort):
    def __init__(self, repo: OrderRepository):
        super().__init__()
        self._repo = repo

    async def create(self, order_id: str, amount: int) -> str:
        await self._repo.save(Order(order_id, amount))
        return order_id


# ── Tests ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hexagonal_full():
    repo = InMemoryOrderRepo()
    payment = FakePaymentGateway()
    uow = InMemoryUoW()
    service = OrderService(repo, payment, uow)

    bus = CommandBus()
    bus.register(CreateOrderCmd, service.create)

    v = Ferronit()

    @v.route("/orders", methods=["POST"])
    async def create_order(req):
        body = await req.json()
        oid = await bus.dispatch(CreateOrderCmd(body["id"], body["amount"]))
        return {"order_id": oid}

    transport = ASGITransport(app=v)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/orders", json={"id": "ord-1", "amount": 100})
        assert r.json() == {"order_id": "ord-1"}


@pytest.mark.asyncio
async def test_hexagonal_input_port_resolves_through_di():
    """Входящий порт → реализация в контейнере; маршрут зависит только от порта."""
    from ferronit.di import Container

    repo = InMemoryOrderRepo()
    c = Container()
    c.singleton(OrderRepository, repo)
    # Интерфейс → реализация: DI строит PortBackedService по его конструктору.
    c.factory(OrderApiPort, PortBackedService)

    v = Ferronit(container=c)

    @v.route("/orders", methods=["POST"])
    async def create_order(req, port: OrderApiPort):
        body = await req.json()
        oid = await port.create(body["id"], body["amount"])
        return {"order_id": oid}

    transport = ASGITransport(app=v)
    async with AsyncClient(transport=transport, base_url="http://test") as cl:
        r = await cl.post("/orders", json={"id": "ord-2", "amount": 50})

    assert r.json() == {"order_id": "ord-2"}
    assert (await repo.get("ord-2")).amount == 50
