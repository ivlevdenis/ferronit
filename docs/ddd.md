# DDD, DI и гексагональная архитектура

Подробный разбор строительных блоков, которые Ferronit поставляет для доменной
логики. Это опциональный слой: минимальное приложение — один `@app.route`.
Общий обзор — в [architecture.md](architecture.md).

## Зачем

Ferronit — не только быстрый ASGI-движок. Он даёт строительные блоки
DDD/CQRS/гексагональной архитектуры, чтобы доменная логика жила отдельно от
HTTP и БД. Это осознанный выбор: фреймворк рассчитан на сервисы, где бизнес-
логика растёт, а не на прототипы из одного файла.

## Гексагональные порты и адаптеры

```python
from ferronit.hexagonal import (
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
from ferronit.ddd import (
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

### Агрегаты в БД

Для сохранения агрегата целиком (заголовок + дочерние таблицы) в одну транзакцию
`ferronit.contrib.rawmodel` даёт пару `Mapper` (агрегат ↔ модель строки) и
обобщённый `Repository[A, M, MAP, P]`. Пример и полный API — в
[data/rawdb.md](data/rawdb.md).

## DI-контейнер

```python
from ferronit import Ferronit
from ferronit.di import Container

app = Ferronit(container=Container())
app.container.singleton(Logger, PrintLogger())
app.container.factory(UserRepository)     # создаётся лениво при первом get()
```

`factory` разрешает зависимости конструктора по type-аннотациям рекурсивно
через контейнер (строковые аннотации по PEP 563 обрабатываются). Несколько
экземпляров одного типа различаются через `Annotated`:

```python
from typing import Annotated
from ferronit.contrib.rawdb import Pool

ShopPool = Annotated[Pool, "shop"]

class UserRepository:
    def __init__(self, pool: ShopPool):
        ...
```

## Жизненный цикл

`@app.lifespan` — одна async-генераторная функция: setup до `yield`, teardown
после. Здесь создают ресурсы и кладут их в контейнер:

```python
from ferronit.contrib.rawdb import create_pool

@app.lifespan
async def lifespan():
    pool = await create_pool(DSN, min_size=1, max_size=16)
    app.container.singleton(ShopPool, pool)
    yield
    await pool.close()
```

Пул создаётся в цикле событий сервера (asyncpg привязан к циклу), доступен
хендлерам через `app.container`, на shutdown закрывается.

## Всё вместе

`app.py` остаётся тонким адаптером над доменным и прикладным слоями:

```python
app = Ferronit()

@app.route("/orders", methods=["POST"])
async def create_order(req):
    data = await req.json()
    await command_bus.dispatch(PlaceOrder(data["order_id"], data["user_id"]))
    return {"ok": True}, 201
```
