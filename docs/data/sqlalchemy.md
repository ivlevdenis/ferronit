# SQLAlchemy — ORM и Core

## Зачем

Когда нужны связи, миграции (Alembic) или знакомый всем ORM — берите
SQLAlchemy-адаптер. Он работает с любым бэкендом (Postgres, SQLite, MySQL).
Компромисс: ORM-mapping — самая дорогая часть запроса, поэтому там, где модель
не нужна, используйте `CoreRepository` (dict вместо ORM-объектов) — это **+46%
на чтении и +24% на записи** против ORM.

```python
from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import DeclarativeBase
from ferronit.contrib.db import RelationalUnitOfWork, create_relational_uow

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

## Core-репозиторий

`uow[table]` (SQLAlchemy `Table`) возвращает `CoreRepository`, работающий с
dict вместо ORM-объектов:

```python
from sqlalchemy import Table, MetaData, Column, Integer, String

metadata = MetaData()
users = Table("users", metadata, Column("id", Integer, primary_key=True), Column("name", String))

async with uow:
    repo = uow[users]                # CoreRepository → dict-строки
    await repo.save({"name": "Ada"})
    rows = await repo.list(limit=50)
```

Правило: ORM — для связей и доменных сущностей, Core — для горячих чтений,
где достаточно dict. Если и Core медленно — см. [сырой asyncpg](rawdb.md) или
[запросы в Rust](rust-query.md).
