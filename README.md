# Velox

High-performance Python ASGI web framework with a Rust core.

## Installation

```bash
pip install velox        # velox-core (Rust-ядро) подтянется автоматически
```

Velox состоит из двух дистрибутивов:

- **`velox`** — Python-слой (ASGI-движок, DDD/CQRS, DI, contrib), чистый Python
- **`velox-core`** — Rust-ядро (роутинг, парсинг запроса, JSON, gzip, CORS), собирается
  [maturin](https://www.maturin.rs)-ом и импортируется как `velox_core`

Ядро собирается с фичей `abi3-py312`, поэтому один wheel (`cp312-abi3`) работает на всех
CPython от 3.12 до 3.14+ — матрица версий Python не нужна. Версии `velox` и `velox-core`
всегда совпадают (проверяется тестом `tests/test_packaging.py`).

### Из исходников

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e .   # dev-установка: ядро соберётся из velox-rs/
./scripts/build_packages.sh                     # оба wheel-а в dist/
```

`[tool.uv.sources]` подменяет `velox-core` на локальный путь `velox-rs/`, поэтому
`pip install -e .` не ходит на PyPI. Ядро отдельно (без переустановки velox):

```bash
cd velox-rs && PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 ../.venv/bin/maturin develop --release
```

## Benchmarks

Velox vs FastAPI, single uvicorn worker, HTTP keep-alive, 5000 requests (median of 3 runs):

| Routes | Velox | FastAPI | Gain |
|---|---|---|---|
| 2 (ASGI in-process) `/` | 6 623 req/s | 5 728 req/s | +16% |
| 2 (ASGI in-process) `/reflect` | 6 461 req/s | 4 514 req/s | +43% |
| 50 (uvicorn, all routes round-robin) | 3 647 req/s | 2 547 req/s | **+43%** |
| 1000 (uvicorn, all routes round-robin) | 3 373 req/s | 1 798 req/s | **+88%** |
| 1000 routes × 500-object payload | 544 req/s | 193 req/s | **+181%** |

### Raw client (ApacheBench, 50 concurrent connections, keep-alive)

httpx (asyncio) client caps at ~3.7k req/s regardless of the server — use a C client for real numbers:

| Scenario | Velox | FastAPI | Gap |
|---|---|---|---|
| uvicorn, 50 routes, `/route0` | 20 390 req/s | 6 095 req/s | **×3.3** |
| uvicorn, 1000 routes, `/route999` | 19 987 req/s | 1 616 req/s | **×12.4** |
| **Granian (Rust ASGI server), 50 routes** | **88 768 req/s** | 11 082 req/s | **×8.0** |

Granian unlocks Velox ×4.35 over uvicorn (88.8k vs 20.4k) but FastAPI only ×1.82 (11.1k vs 6.1k) — the Rust server removes the uvicorn protocol overhead, and Velox's smaller Python footprint benefits the most. 88.8k req/s ≈ 11.3 µs/request — right at the Python handler hot-path limit.

Velox routing does not degrade with route count (20.4k → 20.0k); FastAPI drops 3.8× (6.1k → 1.6k).

**Цена abi3.** Ядро собирается с `abi3-py312` (один wheel на все Python ≥ 3.12) — проверено, что это
почти бесплатно: granian, 1000 маршрутов, ab -n 30000 -c 50 -k, медиана из 5 прогонов —
**abi3 94 762 req/s vs нативное ядро 95 773 req/s (−1.4 %)**, при разбросе между прогонами ±10 %.
Экономия на матрице сборок стоит ~1 % throughput.

### With database (SQLite, handler → ORM/Core → response, ab)

| Server | Operation | Velox ORM | Velox Core | FastAPI |
|---|---|---|---|---|
| Granian | GET (SELECT 100 rows) | 1 616 req/s | **2 362** (+46%) | 1 198 (**+97%**) |
| Granian | POST (INSERT) | 4 012 req/s | **4 961** (+24%) | 3 143 (**+58%**) |
| uvicorn | GET (SELECT 100 rows) | 1 371 req/s | — | 1 063 (+29%) |
| uvicorn | POST (INSERT) | 2 560 req/s | — | 2 121 (+21%) |

The `CoreRepository` (SQLAlchemy Core, dicts instead of ORM objects) adds +46% on reads and +24% on writes; Velox+Core beats FastAPI by ~2× on reads. The DB becomes the shared bottleneck, so the gap narrows — but Velox stays ahead on both reads and writes, and Granian still adds ~20% on top of uvicorn.

### SQLite scaling (what actually works)

Raw ceilings: sync `sqlite3` SELECT 100 rows — 33.5k ops/s, `aiosqlite` — 15.5k. Through HTTP (Granian, 50 concurrent, 1 worker):

| Approach | req/s |
|---|---|
| SQLAlchemy Core (session per request) | 2 329 |
| raw aiosqlite, single connection | 4 345 |
| connection pool (8 conns) | 1 009 ⚠️ |
| sync sqlite3 + `to_thread` | 873 ⚠️ |
| **raw aiosqlite + 4 workers** | **14 016** |

A single connection serializes concurrent requests; a connection pool makes it *worse* (each aiosqlite connection is a thread — GIL contention). **The working pattern: one aiosqlite connection per process + `velox run --workers N`** — scales almost linearly (4 workers = 90% of the aiosqlite ceiling).

### PostgreSQL 18 (Docker, default settings, Granian, ab)

Requires: `docker run -d --name velox-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_USER=postgres -e POSTGRES_DB=postgres -p 5432:5432 postgres:latest`

| Operation | Velox Core | Velox ORM | FastAPI |
|---|---|---|---|
| GET (SELECT 100 rows, LIMIT) | 1 072 req/s | 738 (+45%) | 596 (**+80%**) |
| POST (INSERT + commit) | 639 req/s | 616 | 568 (+13%) |

POST is disk-bound (fsync in Docker ~1.6 ms/commit); GET shows Velox+Core at +80% over FastAPI. `INSERT...RETURNING` is native on Postgres (no slowdown, unlike SQLite).

### Where the time goes (SELECT 100 rows)

| Layer | ops/s | Loss |
|---|---|---|
| sqlite3 (sync C driver) | 33 506 | — (DB ceiling) |
| aiosqlite (async) | 15 080 | −55% |
| SQLAlchemy async ORM | 2 036 | **−87%** |
| Velox GET on Granian | 1 644 | −19% |

The ORM mapping is the real bottleneck (~88% of request time), not the framework — Velox adds only ~19% on top of the ORM layer.

### Payload size (uvicorn, 3 routes, median of 3 runs)

| Response size | Velox | FastAPI | Gain |
|---|---|---|---|
| small (`{"ok":true}`) | 3 609 req/s | 3 669 req/s | ~0% (network-bound) |
| medium (50 objects) | 1 961 req/s | 1 008 req/s | **+94%** |
| big (500 objects) | 546 req/s | 206 req/s | **+165%** |

**Key takeaway:** routing performance does not degrade as routes are added — the Rust `matchit` router is effectively O(1). FastAPI drops 1.8× when going from 50 to 1000 routes; Velox stays flat. On larger payloads the gap widens (2.4-2.7×) — FastAPI's per-request overhead eats the faster `json.dumps` serialization.

### Security (production hardening)

```python
from velox import Velox
from velox.contrib.security import security_headers
from velox.contrib.ratelimit import rate_limit

app = Velox(max_body_size=10 * 1024 * 1024)   # 413 for oversized bodies
app.use(security_headers())                    # nosniff, X-Frame-Options, HSTS, Referrer-Policy
app.use(rate_limit(limit=100, window=60.0))    # 429 sliding window per IP

@app.websocket("/ws", origins=["https://app.example"])  # CSRF-over-WS guard
async def ws(conn):
    await conn.accept()
```

**Rate limiting strategy (ASVS 6.1.1):**
- Login / password reset: `rate_limit(limit=5, window=60.0)` per IP + per account (`key=req.get_header("x-username")`)
- Public API: `rate_limit(limit=100, window=60.0)` per IP; stricter per API key
- Sensitive endpoints (payments, admin): `rate_limit(limit=10, window=60.0)` per user
- Response is `429` with `Retry-After`; limiter is in-memory — use a distributed store (Redis) when scaling to multiple workers

**Input validation rules (ASVS 2.1.1):** the framework validates structural input — path params are typed (`user_id: int` → 404 on non-int), JSON is parsed with recursion/size limits (400/413), query/header parsing is lenient but bounded. Business-rule validation belongs to the application layer.

Security suite: `tests/security/` — 64 tests (injection, XSS, CRLF, CORS, static symlink/dotfiles, WS origin, body limits, rate limiting, anti-fingerprinting) + **ASVS 5.0 L1 compliance** (17 of 70 requirements, see `docs/security/ASVS.md`).

### Run

```bash
velox dev                          # dev server (uvicorn, auto-reload)
velox dev --server granian         # dev on Granian (Rust, ~4x faster)
velox run                          # production (Granian by default, falls back to uvicorn)
velox run --workers 4              # scale with worker processes
```

### Reproduce

```bash
.venv/bin/python bench_real.py              # in-process ASGI comparison
.venv/bin/python bench_network.py 50        # uvicorn, 50 routes
.venv/bin/python bench_network.py 1000      # uvicorn, 1000 routes
.venv/bin/python bench_payload.py           # uvicorn, small/medium/big payloads
.venv/bin/python bench_full.py              # ALL scenarios in one run (full report)
.venv/bin/python bench_db.py granian        # DB benchmark (SQLite, GET/POST, ab)
.venv/bin/python bench_db.py uvicorn
```

Machine: local dev box, Python 3.14, single-core uvicorn worker.
