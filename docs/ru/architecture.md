# Архитектура: DDD, DI и гексагональная

Ferrox поставляет строительные блоки DDD/CQRS/гексагональной архитектуры, чтобы
сервисы могли держать доменную логику свободной от HTTP и кода БД.

## Гексагональные порты и адаптеры

```python
from ferrox.hexagonal import (
    Port, Adapter, Logger, Cache, MessageBus, EventBus, UnitOfWork, ApplicationService,
)
```

- **`Port`** — интерфейс (граница инверсии зависимостей).
- **`Adapter`** — конкретная реализация порта.
- Стандартные порты: `Logger`, `Cache`, `MessageBus`, `EventBus`.
- In-memory адаптеры: `PrintLogger`, `InMemoryCache`, `InMemoryEventBus`.
- **`UnitOfWork`** — транзакционная граница с `commit`/`rollback` и
  контекстным менеджером `async with uow.transaction():`.
- **`ApplicationService`** — базовый класс, держащий unit of work.

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

## Блоки DDD

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

Шины диспетчеризуют по конкретному типу сообщения:

```python
bus = CommandBus()
bus.register(PlaceOrder, handle_place_order)     # sync- или async-хендлер
result = await bus.dispatch(PlaceOrder("1", "u1"))

qb = QueryBus()
qb.register(GetOrder, get_order)
order = await qb.ask(GetOrder("1"))
```

Агрегаты буферизуют `DomainEvent` через `record()` и отдают их через
`pull_events()`.

## DI-контейнер

```python
from ferrox.di import Container

c = Container()
c.singleton(Config, EnvConfig())
c.singleton("pool", await create_raw_pool(dsn))
c.factory("CartService", CartService)     # создаётся лениво при первом get()

svc = c.get("CartService")
```

`factory` разрешает зависимости конструктора по type-аннотациям рекурсивно
через контейнер (строковые аннотации по PEP 563 обрабатываются).

## Конфигурация

```python
from ferrox.config import EnvConfig

config = EnvConfig(prefix="APP_")
db    = config.get("DATABASE_URL")
port  = config.int("PORT", 8000)
debug = config.bool("DEBUG")
hosts = config.list("ALLOWED_HOSTS")       # через запятую
```

## CLI

```bash
ferrox new my_service        # сгенерировать проект
ferrox new my_service --ddd  # с DDD-примером

ferrox dev                    # dev-сервер (uvicorn, reload)
ferrox dev --server granian   # granian, reload
ferrox run                    # продакшен (granian, fallback на uvicorn)
ferrox run --workers 4
```

## Всё вместе

`app.py` остаётся тонким адаптером над доменным и прикладным слоями:

```python
app = Ferrox()

@app.route("/orders", methods=["POST"])
async def create_order(req):
    data = await req.json()
    await command_bus.dispatch(PlaceOrder(data["order_id"], data["user_id"]))
    return {"ok": True}, 201
```
