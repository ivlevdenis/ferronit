# Ferronit

**English** · [Русский](README.ru.md)

[![PyPI](https://img.shields.io/pypi/v/ferronit)](https://pypi.org/project/ferronit/)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://pypi.org/project/ferronit/)
[![License](https://img.shields.io/pypi/l/ferronit)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/ivlevdenis/ferronit)](https://github.com/ivlevdenis/ferronit/stargazers)

A high-performance Python ASGI framework with a Rust core. Routing, request parsing,
JSON, gzip and CORS run in native code; your handlers stay plain Python.

**Documentation:** [`docs/`](docs/README.md) — quick start, routing, requests,
dependency injection, the data layer (three options), DDD/DI, the Rust core,
benchmarks, ASVS. *(Docs are currently written in Russian.)*

## Installation

```bash
pip install ferronit        # installs the Python layer and the native core together (ferronit._core + ferronit.db)
```

Ferronit is a **single package** built with [maturin](https://www.maturin.rs) as a
mixed project. The core (`ferronit._core`) is compiled with the `abi3-py312` feature, so
one wheel works on every CPython from 3.12 to 3.14+. It has no runtime dependencies.

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

Routing is Rust `matchit`, effectively O(1): Ferronit does not degrade as the number of
routes grows, while FastAPI loses up to 4× inside a single routing table. Methodology —
`ab` (C client) only; the full analysis is in [`docs/benchmarks.md`](docs/benchmarks.md).

| Scenario | Ferronit | FastAPI | Gap |
|---|---|---|---|
| uvicorn, 1000 routes | 20 216 req/s | 2 817 req/s | ×7.2 |
| granian, 1000 routes | 94 873 req/s | 3 416 req/s | **×27.8** |

## Data layer

Three options for different jobs — from "request → whole JSON response in Rust" to a
full ORM. Which one to pick: [`docs/data/overview.md`](docs/data/overview.md).

## Development

```bash
poetry install --all-extras             # environment in .venv + all dependencies
PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 poetry run maturin develop --release   # build the Rust core
./scripts/check.sh                      # lint → types → docstrings → core build → tests
poetry run pytest tests/ -q             # 263 tests
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
git tag -a v0.9.1 -m "Ferronit 0.9.1" && git push origin v0.9.1
```

Pushing a `v*` tag triggers `.github/workflows/release.yml`: checks and tests → wheels
(Linux glibc/musl, macOS, Windows) and sdist → GitHub Release with the artifacts → publish
to PyPI (API token from the `PYPI_API_TOKEN` repository secret). If the tag version does not
match `Cargo.toml`/`__init__.py`, the workflow fails before publishing anything.

## Docker

```bash
docker build -t ferronit:0.9.0 .
docker run --rm -p 8000:8000 ferronit:0.9.0
```

## License

Apache License 2.0 — see [LICENSE](LICENSE). Use, modification and distribution are
permitted, including commercial use; copyright notices and the patent grant must be preserved.
