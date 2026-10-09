# Architecture: DDD, DI & hexagonal

Ferrox ships DDD/CQRS/hexagonal building blocks so services can keep domain
logic free of HTTP and database code.

## Hexagonal ports & adapters

```python
from ferrox.hexagonal import (
    Port, Adapter, Logger, Cache, MessageBus, EventBus, UnitOfWork, ApplicationService,
)
```

- **`Port`** — an interface (dependency-inversion boundary).
- **`Adapter`** — a concrete implementation of a port.
- Standard ports: `Logger`, `Cache`, `MessageBus`, `EventBus`.
- In-memory adapters: `PrintLogger`, `InMemoryCache`, `InMemoryEventBus`.
- **`UnitOfWork`** — transactional boundary with `commit`/`rollback` and an
  `async with uow.transaction():` context manager.
- **`ApplicationService`** — base class holding the unit of work.

```python
class PaymentService(ApplicationService):
    def __init__(self, uow, bus, logger):
        super().__init__(uow)
        self._bus = bus
        self._logger = logger

    async def charge(self, order_id):
        async with self.uow.transaction():
            ...
            await self._bus.publish("payments", {"order_id": order_id})
```

## DDD building blocks

```python
from ferrox.ddd import (
    Command, Query, CommandBus, QueryBus, AggregateRoot, DomainEvent, Repository,
)
```

```python
class PlaceOrder(Command):
    __slots__ = ("order_id", "user_id")
    def __init__(self, order_id, user_id):
        self.order_id = order_id
        self.user_id = user_id

class OrderPlaced(DomainEvent):
    __slots__ = ("order_id",)
    def __init__(self, order_id):
        self.order_id = order_id

class Order(AggregateRoot):
    def __init__(self, order_id):
        super().__init__()
        self.order_id = order_id

    def place(self):
        self.record(OrderPlaced(self.order_id))

class GetOrder(Query):
    __slots__ = ("order_id",)
    def __init__(self, order_id):
        self.order_id = order_id
```

Buses dispatch by the message's concrete type:

```python
bus = CommandBus()
bus.register(PlaceOrder, handle_place_order)     # sync or async handler
result = await bus.dispatch(PlaceOrder("1", "u1"))

qb = QueryBus()
qb.register(GetOrder, get_order)
order = await qb.ask(GetOrder("1"))
```

Aggregates buffer `DomainEvent`s via `record()` and release them with
`pull_events()`.

## DI container

```python
from ferrox.di import Container

c = Container()
c.singleton(Config, EnvConfig())
c.singleton("pool", await create_raw_pool(dsn))
c.factory("CartService", CartService)     # constructed lazily on first get()

svc = c.get("CartService")
```

`factory` resolves constructor dependencies by their type annotations
recursively against the container (string annotations via PEP 563 are handled).

## Configuration

```python
from ferrox.config import EnvConfig

config = EnvConfig(prefix="APP_")
db    = config.get("DATABASE_URL")
port  = config.int("PORT", 8000)
debug = config.bool("DEBUG")
hosts = config.list("ALLOWED_HOSTS")       # comma-separated
```

## CLI

```bash
ferrox new my_service        # scaffold a project
ferrox new my_service --ddd  # include a DDD example

ferrox dev                    # dev server (uvicorn, reload)
ferrox dev --server granian   # granian, reload
ferrox run                    # production (granian, falls back to uvicorn)
ferrox run --workers 4
```

## Putting it together

`app.py` stays a thin adapter over the domain and application layers:

```python
app = Ferrox()

@app.route("/orders", methods=["POST"])
async def create_order(req):
    data = await req.json()
    await command_bus.dispatch(PlaceOrder(data["order_id"], data["user_id"]))
    return {"ok": True}, 201
```
