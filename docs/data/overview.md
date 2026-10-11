# Слой данных — rawdb + rawmodel

Ferronit использует **один** способ работы с PostgreSQL: тонкий слой `rawdb` на
asyncpg плюс типизированные модели и query DSL `rawmodel`. Никакого ORM — явный
SQL, bound-параметры, максимум throughput.

| Модуль | Что даёт |
|---|---|
| `ferronit.contrib.rawdb` | `RawUnitOfWork`, `RawRepository`, `Condition`, `create_raw_pool` — сырые dict-строки на asyncpg |
| `ferronit.contrib.rawmodel` | `Model` (неизменяемый msgspec.Struct), `Query`/`select`, `RawModelRepository` — типизированные модели и DSL |

Правило большого пальца: `RawUnitOfWork(pool, readonly=True)` для чтений (пропускает
`BEGIN`, экономит round-trip), `uow.model(User)` для типизированного доступа к строкам.

```python
from ferronit.contrib.rawdb import RawUnitOfWork, create_raw_pool
from ferronit.contrib.rawmodel import Model, select

class User(Model):
    __table__ = "users"        # по умолчанию — плюрализованное имя класса
    id: int = 0
    name: str = ""
    age: int = 0

pool = await create_raw_pool("postgresql://user@host/db", min_size=1, max_size=16)

async with RawUnitOfWork(pool, readonly=True) as uow:
    adults = await uow.model(User).fetch(select(User).where(User.c.age >= 18).limit(50))
```

Подробный разбор API — в [rawdb и rawmodel](rawdb.md).
