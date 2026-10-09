# Getting started

## Installation

```bash
pip install ferrox
```

Ferrox is a **single package** built with [maturin](https://www.maturin.rs) as a
mixed Rust/Python project. Installing it gives you:

- the Python layer (`ferrox`) — ASGI engine, DDD/CQRS, DI, contrib;
- the native core (`ferrox._core`) — routing, request parsing, JSON, gzip, CORS;
- the data layer (`ferrox.db`) — PostgreSQL → JSON fully in Rust.

The core is built with the `abi3-py312` feature, so one wheel works on every
CPython 3.12+ — no Python version matrix.

Optional extras:

```bash
pip install "ferrox[server]"     # granian, uvicorn
pip install "ferrox[postgres]"   # asyncpg, msgspec (raw data layer)
pip install "ferrox[db]"         # SQLAlchemy 2.0
pip install "ferrox[pydantic]"   # pydantic v2
```

## First application

```python
# app.py
from ferrox import Ferrox

app = Ferrox()

@app.route("/")
async def index():
    return {"service": "ferrox", "ok": True}

@app.route("/hello/{name}")
async def hello(name: str):
    return {"hello": name}
```

`app` is itself the ASGI callable — pass it to any server:

```bash
granian --interface asgi --port 8000 app:app
uvicorn --port 8000 app:app
```

or use the bundled CLI (see [CLI](architecture.md#cli)):

```bash
ferrox dev                       # uvicorn with reload
ferrox dev --server granian      # granian with reload
ferrox run                       # granian (falls back to uvicorn)
ferrox run --workers 4           # scale across processes
```

## Project layout

There is no enforced layout. A typical Ferrox service follows the DDD-friendly
structure the framework encourages:

```
my_service/
  app.py              # Ferrox instance + HTTP routes (thin adapters)
  domain/             # aggregates, entities, domain events (zero deps)
  application/        # commands, queries, application services
  infrastructure/     # repositories, buses (Postgres, Kafka, ...)
```

See [DDD, DI & hexagonal](architecture.md) for the building blocks.

## Building from source

```bash
uv venv .venv
uv pip install -e .                     # maturin builds ferrox + ferrox._core
./scripts/build_packages.sh             # one wheel into dist/
```

Rebuild only the Rust core during development:

```bash
PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 .venv/bin/maturin develop --release
```

## Docker

```bash
docker build -t ferrox:0.8.0 .
docker run --rm -p 8000:8000 ferrox:0.8.0

# your own app
docker run --rm -p 8000:8000 -v "$PWD:/app" -e APP=app:app ferrox:0.8.0
```

Container variables: `APP` (`module:attr`, default `demo_app:app`), `SERVER`
(`granian` | `uvicorn`), `HOST`, `PORT`, `WORKERS`, `RELOAD=1`.
