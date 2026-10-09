# Ferrox — Documentation

Ferrox is a high-performance Python ASGI web framework with a **Rust core**.
Routing, request parsing, JSON serialisation, gzip and CORS run in native code
(`ferrox._core`); your handlers stay ordinary Python.

## Documentation index

### Get started
- [Getting started](getting-started.md) — install, first app, running, project layout
- [Routing & handlers](routing.md) — routes, path parameters, return values, responses
- [Requests](requests.md) — `Request`: query, headers, cookies, body, JSON, forms, files
- [Dependency injection](injection.md) — typed parameters, `Header`, lists, unions, body models

### Features
- [Models & serialisation](models.md) — `msgspec`, Pydantic, dataclasses, codecs
- [OpenAPI](openapi.md) — automatic schema generation
- [WebSocket](websocket.md) — text, binary and JSON messaging
- [Middleware & contrib](middleware.md) — CORS, security headers, rate limiting, tracing, health, static files

### Data layer
- [Database](database.md) — `ferrox.db` (Rust), SQLAlchemy adapter, raw asyncpg layer
- [Models & query DSL](rawmodel.md) — `Model`, `Query`, `where` DSL, repositories

### Architecture
- [DDD, DI & hexagonal](architecture.md) — ports/adapters, buses, container, config, CLI
- [Rust core](rust-core.md) — what lives in `ferrox._core` and why

### Reference
- [Benchmarks](benchmarks.md) — measured numbers and how to reproduce

---

## The idea in one screen

```python
from ferrox import Ferrox
from ferrox.contrib.rawdb import RawUnitOfWork, create_raw_pool
from ferrox.contrib.rawmodel import Model, select

app = Ferrox(max_body_size=10 * 1024 * 1024)

class User(Model):
    __table__ = "users"
    id: int = 0
    name: str = ""
    age: int = 0

_pool = None

@app.route("/users")
async def list_users(age: int = 18):
    async with RawUnitOfWork(_pool, readonly=True) as uow:
        repo = uow.model(User)
        users = await repo.fetch(select(User).where(User.c.age > age))
    return {"users": users}
```

- **Zero runtime dependencies** in the core — the Rust extension ships inside the
  same wheel.
- **`granian`-ready** — the thin Python layer means Ferrox keeps ~⅔ of raw
  Granian throughput where other frameworks keep ~¼.
