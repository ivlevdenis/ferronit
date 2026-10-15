# Ferronit

**English** · [Русский](README.ru.md)

[![PyPI](https://img.shields.io/pypi/v/ferronit)](https://pypi.org/project/ferronit/)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://pypi.org/project/ferronit/)
[![License](https://img.shields.io/pypi/l/ferronit)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/ivlevdenis/ferronit)](https://github.com/ivlevdenis/ferronit/stargazers)

> ⚠️ **Early development — not production-ready.** The API and internals are still
> changing; use at your own risk.

Your legacy Python service is hitting its performance ceiling. Ferronit moves the hot path —
routing, request parsing, JSON, gzip, CORS, PostgreSQL — into a Rust core, while your handlers
stay plain Python. A migration, not a rewrite.

**Documentation:** [`docs/`](docs/README.md) — quick start, routing, requests,
dependency injection, the data layer (rawdb + rawmodel), DDD/DI, the Rust core,
benchmarks, ASVS. *(Docs are currently written in Russian.)*

## Installation

```bash
pip install ferronit        # installs the Python layer and the native core together
```

Ferronit is a **single package** built with [maturin](https://www.maturin.rs) as a
mixed project. The core (`ferronit._core`) is compiled with the `abi3-py312` feature, so
one wheel works on every CPython from 3.12 to 3.15+. Its only runtime dependencies are
`msgspec` (fast JSON/validation) and `asyncpg` (the rawdb/rawmodel data layer).

```python
# app.py
from ferronit import Ferronit

app = Ferronit()

@app.route("/hello/{name}")
async def hello(name: str):
    return {"hello": name}
```

```bash
ferronit dev                          # dev server (uvicorn, reload)
ferronit dev --server granian         # granian (Rust, ~4× faster)
ferronit run --workers 4              # production: granian, uvicorn fallback
```

## Why it is fast

Routing is Rust `matchit`, effectively O(1). On database reads Ferronit beats the
competition with positional row-mapping (`rawmodel`) + `msgspec`. Methodology — `ab`
(C client) only, granian, 1 worker; full breakdown in
[`docs/benchmarks.md`](docs/benchmarks.md).

| Scenario | Ferronit | BlackSheep | FastAPI |
|---|---|---|---|
| GET /ping (dict) | 98 197 req/s | 93 523 req/s | 19 036 req/s |
| GET /rows?limit=100 | 8 350 req/s | 8 065 req/s | 956 req/s |
| GET /rows?limit=1000 | 2 334 req/s | 1 780 req/s | 158 req/s |

## Data layer

One thin layer over the driver — `rawdb` + `rawmodel`: PostgreSQL (asyncpg), MySQL (aiomysql)
and SQLite (aiosqlite) behind the same API. Declarative models, a query DSL (`select`), relations,
a Unit of Work — no ORM. SQL is built from an expression tree and compiled per dialect. Details:
[`docs/data/overview.md`](docs/data/overview.md).

```python
from typing import Annotated
from ferronit import Ferronit
from ferronit.contrib.rawdb import create_pool
from ferronit.contrib.rawmodel import Model, Relation

class OrderItem(Model):
    __table__ = "order_items"
    id: int = 0
    order_id: int = 0
    name: str = ""

class Order(Model):
    __table__ = "orders"
    id: int = 0
    items: Annotated[list[OrderItem], Relation("order_id")] = []   # 1:N, always loaded

app = Ferronit()

@app.route("/orders")
async def orders():
    pool = await create_pool("postgresql://user:pass@localhost/db")  # usually lifespan/DI
    async with pool.uow(readonly=True) as uow:
        rows = await uow.model(Order).list(limit=10)
    return {"orders": rows}   # items nested in JSON, one WHERE fk = ANY($1)
```

## Development

```bash
poetry install --all-extras             # environment in .venv + all dependencies
PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 poetry run maturin develop --release   # build the Rust core
./scripts/check.sh                      # lint → types → docstrings → core build → tests
poetry run pytest tests/ -q             # 265 tests
```

Poetry manages the environment (`poetry.toml` keeps the venv inside the project), while the
wheel carrying the Rust core is built by maturin (see `[tool.maturin]` in `pyproject.toml`),
so Poetry itself never installs the project.

- **[AGENTS.md](AGENTS.md)** — guidance for code agents (Claude Code, Codex, Cursor, …).
- **`examples/`** — minimal, DI, LLM + SSE, RAG, WebSocket; `examples/app.py` is the DDD example.
- The package ships `py.typed`; type stubs for the Rust core live in `ferronit/_core.pyi`.
- `bench/` — the measurement rig behind every number above; not part of the package.

## Release

```bash
# the version lives in two files: ferronit/__init__.py and ferronit-rs/Cargo.toml
git tag -a v0.20.0 -m "Ferronit 0.20.0" && git push origin v0.20.0
```

Pushing a `v*` tag triggers `.github/workflows/release.yml`: checks and tests → wheels
(Linux glibc/musl, macOS, Windows) and sdist → GitHub Release with the artifacts → publish
to PyPI (API token from the `PYPI_API_TOKEN` repository secret). If the tag version does not
match `Cargo.toml`/`__init__.py`, the workflow fails before publishing anything.

## Docker

```bash
docker build -t ferronit:0.20.0 .
docker run --rm -p 8000:8000 ferronit:0.20.0
```

## License

Apache License 2.0 — see [LICENSE](LICENSE). Use, modification and distribution are
permitted, including commercial use; copyright notices and the patent grant must be preserved.
