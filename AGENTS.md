# AGENTS.md — Ferronit

Instructions for coding agents (Claude Code, Codex, Cursor, Copilot, Pi, Gemini CLI, etc.).
Read it in full before changing any code.

## What this is

Ferronit is a high-performance Python ASGI framework whose infrastructure layer is written in
Rust: **routing, request parsing, gzip and CORS** (JSON stays in Python via msgspec). Business
logic remains plain Python: DDD / CQRS, hexagonal (ports and adapters), a DI container.

Version 0.24.0, requires Python ≥ 3.12. Speed (local `ab -c 50 -n 30000 -k`, granian RSGI,
1 worker): ~125–130k req/s on `GET /ping`, ~×1.4 vs BlackSheep, ~×6–11 vs FastAPI (uvicorn).
Official numbers and methodology live in `ferronit-bench/docs/benchmarks.md`; do not copy them
here. A single `ab` process tops out at ~200k req/s on 20 vCPU: if the server is faster, run
parallel `ab` clients, otherwise you are measuring the client.

The repository is **one distribution** `ferronit` (maturin mixed):

| Directory | What it is |
|---|---|
| `ferronit/` | Python package: ASGI engine, DDD, DI, contrib + native core `ferronit._core` |
| `ferronit-rs/` | Rust crate (routing/gzip/CORS), built as `ferronit._core` |

Build is maturin (`abi3-py312`), one wheel: `pip install ferronit` installs both the Python layer
and the core.

## Repository map

```
ferronit/
├── __init__.py            # Ferronit, Request, Response, Headers, WebSocket, __version__
├── core/
│   ├── app.py             # ASGI engine: routes, middleware, gzip, CORS, lifespan
│   ├── request.py         # Request facade (headers/query/cookies in Rust), BodyTooLarge
│   ├── headers.py         # Headers — alias for the Rust class (RFC rules live in the core)
│   └── response.py        # Response, JSONResponse, StreamingResponse, TextResponse, set_cookie
├── binding.py             # handler parameter binding: request + container (resolved at registration)
├── middleware.py          # middleware stack
├── di.py                  # Container: singletons/factories by type or string
├── ddd.py                 # AggregateRoot, Command/Query, CommandBus, Repository
├── hexagonal.py           # Port, Adapter, UnitOfWork, ApplicationService + Logger/Cache/EventBus
├── openapi.py             # OpenAPI generation from type hints
├── websocket.py           # WebSocket handlers, Origin guard
├── config.py              # EnvConfig: typed environment variables
├── cli.py                 # ferronit new / dev / run + scaffold project templates
└── contrib/               # infrastructure adapters (optional dependencies)
    ├── sql.py             # expression tree + dialects (POSTGRES/SQLITE/MYSQL) and SQL compilation
    ├── drivers.py         # connection/pool adapters: asyncpg (PostgreSQL), aiomysql (MySQL), aiosqlite (SQLite) — all three drivers are extras
    ├── rawdb.py           # thin layer over the driver: RawRepository/RawUnitOfWork/Condition (no ORM)
    ├── rawmodel.py        # declarative models + Query/select + relations (Relation) on top of rawdb
    ├── llm.py             # LlmPort + OpenAiAdapter, ClaudeAdapter, MockLlmAdapter
    ├── vectordb.py        # VectorDbPort + Qdrant, Chroma, Mock
    ├── kafka.py           # KafkaMessageBus (aiokafka)
    ├── cors.py, security.py, ratelimit.py, staticfiles.py, tracing.py, health.py, logging.py
    └── pydantic/          # pydantic v2 codec

ferronit-rs/src/lib.rs        # Rust: Router, Request (headers/cookies/query), Headers,
                               #    build_set_cookie, parse_headers, first_header, gzip, CORS, multipart
pyproject.toml                # metadata, extras, [tool.poetry], [tool.maturin], ruff/mypy
poetry.toml, poetry.lock      # Poetry environment (venv inside .venv/) and locked dependencies
tests/                     # 545 tests (+9 skipped); tests/security/ — 65 (incl. 14 ASVS L1)
examples/                  # minimal, DI, LLM+SSE, RAG, WebSocket + README
docker/                    # Dockerfile glue: demo_app.py, entrypoint.sh
docs/                      # documentation: README.md (hub), data/ (data layer),
                           #   security/ASVS.md (OWASP ASVS 5.0 L1)
```

Benchmarks and methodology live in a separate repository
[`ferronit-bench`](https://github.com/ivlevdenis/ferronit-bench) (`docs/benchmarks.md`,
`api_bench.py`, charts). There is no benchmark stand in this repository — do not add bench
scripts to the root. **HTTP req/s is measured with `ab` only** (C client): a Python client tops
out at ~3.7k req/s and measures itself. **Ferronit is measured and profiled through RSGI only**
(`granian --interface rsgi`, `__rsgi__`): the ASGI entry `__call__` is a different hot path
(two `send` events instead of `response_bytes`, a different request build). **The primary
competitor to compare against is BlackSheep**; other stacks are extra reference points. Add new
stands and numbers to `ferronit-bench`, not here.

## Commands

```bash
# development install (Poetry owns the environment, maturin builds the Rust core)
poetry install --all-extras                              # venv in .venv/ + dependencies
PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 poetry run maturin develop --release   # core, from the ROOT

# everything at once: lint → types → docstrings → core build → tests
./scripts/check.sh

# individually (`.venv/bin/...` works too — the venv lives inside the project)
poetry run pytest tests/ -q                    # 545 tests, ~3 s
poetry run pytest tests/security -q            # security suite + ASVS L1
poetry run ruff check ferronit tests           # lint
poetry run mypy ferronit                       # types
poetry run python scripts/agent_readiness.py   # docstring coverage (currently 100%)
poetry run python scripts/agent_readiness.py --missing   # which entities lack a docstring

# Rust core only (after changes in ferronit-rs/) — run from the repository root:
PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 poetry run maturin develop --release
# Do NOT run it from ferronit-rs/: maturin will miss the root [tool.maturin], install a stray
# ferronit_core package and leave ferronit/_core.abi3.so stale.

# wheel + sdist into dist/
./scripts/build_packages.sh

# dev and prod
poetry run ferronit dev --server granian   # dev server with reload
poetry run ferronit run --workers 4        # prod: Granian, uvicorn fallback

# docker
docker build -t ferronit:0.24.0 . && ./scripts/docker_smoke.sh
```

The Postgres tests (`tests/test_postgres_real.py`, 4 tests) need a container on :5432:

```bash
docker start ferronit-pg   # if the container does not exist yet:
docker run -d --name ferronit-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_USER=postgres \
  -e POSTGRES_DB=postgres -p 5432:5432 postgres:18
```

## Invariants — DO NOT break

1. **Versions are synchronised**: `ferronit/__init__.py::__version__` == `ferronit-rs/Cargo.toml::version`
   == the `ferronit` distribution metadata. Enforced by `tests/test_packaging.py`; a release edits
   **two** files.
2. **`Cargo.lock` is committed** — change pyo3 features only via `Cargo.toml` + a rebuild.
3. **`abi3-py312`, never downgrade**: `PyString::to_str` needs `PyUnicode_AsUTF8AndSize`, which is
   absent from the limited API before 3.10; the Python layer already requires ≥ 3.12 anyway.
   One wheel covers 3.12+.
4. **Python core: stdlib + `ferronit._core` + `msgspec` — the only required dependency.**
   Database drivers are extras (`postgres` → asyncpg, `mysql` → aiomysql, `sqlite` → aiosqlite) and
   are imported lazily (inside `create_pool`), so `import ferronit.contrib.rawdb` works without them;
   everything else is optional extras (`server`, `pydantic`).
5. **JSON and response assembly stay in Python**: serialisation is the `msgspec` C encoder (stdlib
   `json.dumps(default=str)` fallback), body parsing is `msgspec.json.decode`. There is no Rust
   `Response` class at all (removed as dead code); the native core exposes `Request`, `Headers`,
   `Router`, `FerronitApp` and the functions `build_set_cookie`/`parse_headers`/`first_header`.
6. **PyO3 `#[getter]` strips the `get_` prefix**: Rust `fn get_status()` becomes `.status` in
   Python. **Response headers go on the wire lowercased** (an ASGI requirement), so tests compare
   names case-insensitively; `Headers[...]` lookup is case-insensitive.
7. **Python 3.14+**: build the core only with `PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1`.
8. **Handlers must be async**: `binding.plan()` raises `TypeError` for a synchronous callable at
   registration time, so the event loop is never blocked. Middleware may be sync or async —
   `Middleware.wrap` checks `hasattr(result, "__await__")`.
9. **Set `X-Trace-Id` in `Ferronit.__call__` AFTER `_to_response`** — otherwise the header is lost
   on dict/pydantic returns.
10. **What `_to_response` understands**: `msgspec.Struct` and a non-empty list of them (JSON via
    the msgspec C encoder, stdlib `json` fallback), `dict` and `list` (JSON via msgspec), a
    `(payload, status)` tuple — the documented way to set a status
    (`return {"error": "not found"}, 404`), `Response`, `str` (text/plain), an object with
    `model_dump()` / a dataclass (JSON), an async iterator (streaming). For anything non-standard,
    build a `Response` explicitly.
11. **Public behaviour changes only together with a test.** Failing test first, then code.
12. **New public entities need a docstring** (checked by `scripts/agent_readiness.py`, threshold
    90% in `scripts/check.sh`).
13. **Poetry owns the environment, maturin builds the wheel**: `poetry install --all-extras` +
    `poetry run maturin develop --release`. `poetry.lock` is committed — when editing dependencies
    run `poetry lock`; never delete the file. Poetry does not install the project itself
    (`package-mode = false`) because the build is native.
14. **`Command` and `Query` are `msgspec.Struct`**: declare fields through annotations; msgspec
    forbids `__init__` and `__slots__` (`Struct types cannot define __init__`). Messages are
    serialisable (`msgspec.json.encode/decode`), so a command can be sent to an external bus, not
    only to the in-process `CommandBus`.
15. **Never swallow errors.** `except Exception: pass`, `except: continue`, or `contextlib.suppress`
    without justification is a defect; ruff `S110`/`S112` catch it (enabled in `select`). Catch only
    the specific expected type; if it is expected, either handle it meaningfully, log it
    (`logger.debug`/`warning`/`error`), or re-raise. A broad `except Exception` is allowed only at
    the request boundary (mapping to 500 with a log) and in cleanup paths where a failure must not
    mask the original error. A deliberate no-op needs a comment and `# noqa: S110` (the only such
    case today is the best-effort write in `contrib/logging.py`). Never swallow an exception "so it
    does not crash": that is exactly how the "empty headers on RSGI" bug survived — `bytes(str)`
    raised and `except Exception: pass` hid it.

16. **The header container is a `#[pyclass] Headers` in the core.** `ferronit/core/headers.py`
    only exposes the alias and the `HeaderInput` type. The RFC 9110 §5.5 rules (CR/LF/NUL → SP,
    other CTLs → `ValueError`, latin-1, OWS) and RFC 6265 (`cookie-octet`, `set-cookie` is never
    joined) live in Rust; do not duplicate them in Python.
17. **`Response.headers` is a sequence of field lines, not a dict.** Order and duplicates are
    preserved; repeated fields are appended with `headers.add(...)`, not `headers[...] = ...`
    (assignment replaces every line); cookies go through `set_cookie()/delete_cookie()`.
18. **`Request.headers` combines repeats** in wire order: `", "` for list-based fields, `"; "` for
    `cookie` (RFC 9113 §8.2.3), and `set-cookie` keeps its first line. Raw values are available via
    `get_header()` (first) and `get_all()` (all). Names are compared as raw bytes; values are
    decoded only for pairs whose name matched.
19. **Before a `Request` exists, look a header up only through `_core.first_header(raw, name)`**
    (it accepts a list of pairs and `rsgi.Headers`). Do not add your own `scope["headers"]` scans.
20. **Changing the public `_core` surface requires updating `ferronit/_core.pyi`**, otherwise mypy
    fails on import (by design).

## Code style

- **Types are mandatory** on public functions and methods (arguments + return). The package is
  marked `py.typed`, annotations are trustworthy — mypy and agent tools rely on them.
- **Docstrings are English, Google-style**, on every public entity: the first line is a short
  description, followed by `Args:` / `Returns:` / `Raises:` when relevant. Inline code comments may
  stay in Russian if that is clearer.
- **Public API is declared in `__all__`** of each module.
- Private names start with `_`.
- Formatting: ruff (line-length 100, target py312), import sorting enabled.
- **No error swallowing** (invariant 15): `except Exception: pass` is forbidden and caught by ruff
  `S110`/`S112`; an expected exception means a specific type plus handling/logging, an unexpected
  one means `raise`. If ignoring is truly necessary (best-effort cleanup, `ProcessLookupError`
  while reading `/proc`), leave a comment and `# noqa: S110` explaining why it is safe.
- Tests: pytest, `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed), files
  `tests/test_<topic>.py`, security tests under `tests/security/`.

## Where to go for a task

| Task | File |
|---|---|
| Routing, gzip, CORS in the core | `ferronit-rs/src/lib.rs` (Rust, rebuild required) |
| ASGI engine, route registration, errors | `ferronit/core/app.py` |
| Request headers, cookies, query | `ferronit-rs/src/lib.rs` (`Request`), facade — `ferronit/core/request.py` |
| Response headers, cookies, validation | `ferronit-rs/src/lib.rs` (`Headers`, `build_set_cookie`), facade — `response.py` |
| Responses, streaming | `ferronit/core/response.py` |
| Handler parameter types | `ferronit/binding.py` |
| DDD / CQRS | `ferronit/ddd.py` |
| Ports and adapters | `ferronit/hexagonal.py`, `ferronit/contrib/*` |
| Database access | `ferronit/contrib/rawdb.py` + `rawmodel.py` — one ORM-free layer for three engines: PostgreSQL (asyncpg), MySQL (aiomysql), SQLite (aiosqlite); drivers are optional (extras) |
| LLM / vector search | `ferronit/contrib/llm.py`, `ferronit/contrib/vectordb.py` |
| Security (headers, limits, rate limit) | `ferronit/contrib/security.py`, `ratelimit.py`, `docs/security/ASVS.md` |
| Logging (JSON/text, access/error log) | `ferronit/contrib/logging.py`, `docs/logging.md` |
| Scaffolding new projects | `ferronit/cli.py` (templates at the top of the file) |

## Saving context

- "Where does X live / how is it wired" questions → run `graphify query "<question>"` first (the
  graph in `graphify-out/` is ready), then `graphify explain "<concept>"`, and only then grep.
- After code changes: `graphify update .` (AST-only, no API calls).
- Architecture map: `docs/architecture.html`. Security requirements: `docs/security/ASVS.md`.

## Work boundaries

- **Commit only on an explicit user request.** Finished work ≠ permission to commit. By default:
  make the changes, show `git status` / `git diff --stat`, say "changes are not committed", and
  wait. Never "also" commit, amend, push, switch branches, or revert commits. The technical guard is
  `.githooks/pre-commit` (a commit fails without `FERRONIT_COMMIT_OK=1`); bypass it only when
  permission genuinely exists.
- Do not add new external dependencies to `ferronit/` without need (msgspec is the only required
  one; database drivers and the rest are optional extras in `pyproject.toml`, imported inside the
  factory so the module imports without the extra).
- The benchmark stand is a separate repository, `ferronit-bench`; do not add bench scripts here.
- Never commit: `.venv/`, `dist/`, `ferronit-rs/target/`, `*.session`, `.env`.
- Do not rewrite numbers in the README without a real measurement (`ab -n 30000 -c 50 -k`,
  Granian/uvicorn with 1 worker, median of runs).
- Never downgrade abi3 or change pyo3 features "in place" — only via `Cargo.toml` and a rebuild.

## Known limitations

- **Lifecycle is a single `@app.lifespan` function** (async generator: setup → `yield` →
  teardown); resources are registered in `app.container.singleton(...)` at startup. There are no
  separate `on_startup`/`on_shutdown` hooks.
- **Logging is optional** (`ferronit.contrib.logging`): a non-blocking pipeline (JSON or
  human-readable) with delivery guarantees (`flush()`/`shutdown()` + `atexit`), an access log
  (`event="access"`, `method/path/status/duration_ms`) and an error log (`event="error"` + traceback).
  Inject the logger as `Ferronit(logger=Logger(LogConfig(...)), access_log=True)` from
  `ferronit.contrib.logging`.
- The in-memory rate limiter multiplies limits by the number of workers when `--workers > 1`
  (multi-process deployments need a Redis backend).
- `EnvConfig.int/float/bool` silently return the default on a parse error — there is no "fail-fast".
- There is no CI (the repository has no remote); run `./scripts/check.sh` locally before committing.
