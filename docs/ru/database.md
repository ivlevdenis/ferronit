# База данных

Ferrox предлагает три слоя данных с разными компромиссами. Выбирайте тот, что
подходит горячему пути.

| Слой | Модуль | Когда использовать |
|---|---|---|
| Rust-движок запросов | `ferrox.db` | read-heavy эндпоинты, большие выборки |
| Сырой asyncpg | `ferrox.contrib.rawdb` + `rawmodel` | максимальный throughput, типизированные модели + SQL DSL |
| SQLAlchemy | `ferrox.contrib.db` | полный ORM, связи, миграции |

## `ferrox.db` — запросы целиком в Rust

Для read-heavy хендлеров, отдающих большие выборки, выполняйте запрос и
JSON-кодирование в Rust (фоновый tokio-рантайм), минуя Python-драйвер.

```python
import asyncio
import ferrox

ferrox.db.connect("postgresql://user:pass@host/db", 16)   # один раз на старте

@app.route("/report")
async def report():
    body = await asyncio.to_thread(
        ferrox.db.query_json,
        "SELECT id, name FROM users ORDER BY id LIMIT 1000",
        [],
    )
    return Response(body, content_type="application/json")
```

- `connect(dsn, pool_size)` — один вызов при старте.
- `query_json(sql, params)` — синхронная; снимает GIL и ждёт tokio-рантайм,
  поэтому вызывайте её через `asyncio.to_thread`.

## Сырой asyncpg-слой — `ferrox.contrib.rawdb`

Тонкий, явный, без query builder. Фильтры — SQL-фрагменты с **bound-параметрами**;
строки приходят как dict. Примерно в 3 раза быстрее SQLAlchemy Core на тех же
запросах.

```python
from ferrox.contrib.rawdb import RawUnitOfWork, Condition, create_raw_pool

pool = await create_raw_pool("postgresql://user:pass@host/db", min_size=1, max_size=16)

async with RawUnitOfWork(pool) as uow:            # BEGIN / COMMIT (или rollback)
    repo = uow["users"]                           # имя таблицы → RawRepository
    row = await repo.save({"name": "Ada"})        # → {"name": "Ada", "id": 1}
    await repo.update(1, {"name": "Grace"})
    users = await repo.list(Condition("age > $1", [18]), limit=50)
    await repo.delete(1)
```

- `RawUnitOfWork(pool, readonly=False)` — одно соединение, одна транзакция.
  `readonly=True` пропускает `BEGIN` (экономия одного round-trip) для read-only.
- `Condition(sql, params)` — фрагмент с PostgreSQL-плейсхолдерами (`$1`…).
  Комбинируется операторами:

```python
c = Condition("age > $1", [18]) & Condition("name ILIKE $1", ["%a%"])
c = Condition("status = $1", ["a"]) | Condition("status = $1", ["b"])
c = ~Condition("email IS NULL")          # NOT (...)
```

Плейсхолдеры перенумеровываются автоматически при комбинировании.

Жизненный цикл: создавайте пул **внутри** event loop сервера; используйте
**один** `RawUnitOfWork` на запрос.

## SQLAlchemy-слой — `ferrox.contrib.db`

```python
from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import DeclarativeBase
from ferrox.contrib.db import RelationalUnitOfWork, create_relational_uow

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String)

uow = create_relational_uow("postgresql+asyncpg://user:pass@host/db")

async with uow:
    repo = uow[User]                 # репозиторий ORM-сущностей
    await repo.save(User(name="Ada"))
    user = await repo.get(1)
```

`uow[table]` (SQLAlchemy `Table`) возвращает `CoreRepository`, работающий с
dict вместо ORM-объектов — +46% на чтении и +24% на записи против ORM.
