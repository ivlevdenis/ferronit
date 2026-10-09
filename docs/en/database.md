# Database

Ferrox offers three data layers with different trade-offs. Pick the one that
matches the hot path.

| Layer | Module | When to use |
|---|---|---|
| Rust query engine | `ferrox.db` | read-heavy endpoints, big result sets |
| Raw asyncpg | `ferrox.contrib.rawdb` + `rawmodel` | maximum throughput, typed models + SQL DSL |
| SQLAlchemy | `ferrox.contrib.db` | full ORM, relationships, migrations |

## `ferrox.db` — queries entirely in Rust

For read-heavy handlers that return large selections, run the query and the
JSON encoding in Rust (a background tokio runtime), skipping the Python driver.

```python
import asyncio
import ferrox

ferrox.db.connect("postgresql://user:pass@host/db", 16)   # once at startup

@app.route("/report")
async def report():
    body = await asyncio.to_thread(
        ferrox.db.query_json,
        "SELECT id, name FROM users ORDER BY id LIMIT 1000",
        [],
    )
    return Response(body, content_type="application/json")
```

- `connect(dsn, pool_size)` — one call at startup.
- `query_json(sql, params)` — synchronous; releases the GIL and waits on the
  tokio runtime, so call it via `asyncio.to_thread`.

## Raw asyncpg layer — `ferrox.contrib.rawdb`

Thin, explicit, no query builder. Filters are SQL fragments with **bound
parameters**; rows come back as dicts. Roughly 3× the SQLAlchemy Core
throughput on the same queries.

```python
from ferrox.contrib.rawdb import RawUnitOfWork, Condition, create_raw_pool

pool = await create_raw_pool("postgresql://user:pass@host/db", min_size=1, max_size=16)

async with RawUnitOfWork(pool) as uow:            # BEGIN / COMMIT (or rollback)
    repo = uow["users"]                           # table name → RawRepository
    row = await repo.save({"name": "Ada"})        # → {"name": "Ada", "id": 1}
    await repo.update(1, {"name": "Grace"})
    users = await repo.list(Condition("age > $1", [18]), limit=50)
    await repo.delete(1)
```

- `RawUnitOfWork(pool, readonly=False)` — one connection, one transaction.
  `readonly=True` skips `BEGIN` (one round-trip saved) for read-only units.
- `Condition(sql, params)` — a fragment with PostgreSQL placeholders (`$1`…).
  Combine with operators:

```python
c = Condition("age > $1", [18]) & Condition("name ILIKE $1", ["%a%"])
c = Condition("status = $1", ["a"]) | Condition("status = $1", ["b"])
c = ~Condition("email IS NULL")          # NOT (...)
```

Placeholders are renumbered automatically when conditions combine.

Lifecycle notes: create the pool **inside** the event loop the server runs in;
use **one** `RawUnitOfWork` per request.

## SQLAlchemy layer — `ferrox.contrib.db`

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
    repo = uow[User]                 # ORM entity repository
    await repo.save(User(name="Ada"))
    user = await repo.get(1)
```

`uow[table]` (a SQLAlchemy `Table`) returns a `CoreRepository` that works with
dicts instead of ORM objects — +46% on reads and +24% on writes versus the ORM.
