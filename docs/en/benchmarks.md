# Benchmarks

Methodology is fixed: **ApacheBench only** (`ab -n N -c 50 -k`). Python HTTP
clients (httpx/asyncio) top out at ~3.7k req/s regardless of server, so they
measure the client, not the framework. All apps run on the same server, 1
worker, best of several runs, keep-alive. Full harness: `bench/` directory.

## Routing: Ferrox vs FastAPI

| Scenario | Ferrox | FastAPI | Gap |
|---|---|---|---|
| uvicorn, 50 routes | 20 060 | 6 589 | ×3.0 |
| uvicorn, 1000 routes | 20 216 | 2 817 | ×7.2 |
| granian, 50 routes | 96 298 | 12 135 | ×7.9 |
| granian, 1000 routes | 94 873 | 3 416 | ×27.8 |

- Ferrox does **not** degrade as the route table grows (20 060 → 20 216 on
  uvicorn; 96 298 → 94 873 on granian for 50 → 1000 routes). FastAPI slows up
  to 4× *within a single* route table.
- Granian removes uvicorn's protocol overhead: Ferrox ×4.8, FastAPI ×1.8.

## Rows: Ferrox vs Litestar vs FastAPI vs Django

Same PostgreSQL, granian, 1 worker, `ab -c 50 -k` (1 / 100 / 1000 rows):

| Variant | 1 row | 100 rows | 1000 rows | /ping |
|---|---|---|---|---|
| Ferrox + asyncpg (dict) | 15 540 | 7 849 | 1 275 | 91 935 |
| Ferrox + msgspec | 14 611 | 10 664 | 2 279 | 91 642 |
| Ferrox + rawmodel | 13 959 | 9 011 | 2 199 | 91 208 |
| Litestar + msgspec | 11 138 | 8 218 | 2 032 | 36 636 |
| Litestar + ORM | 2 474 | 1 028 | 283 | 37 503 |
| FastAPI + ORM | 2 072 | 680 | 161 | 24 408 |
| Django + ORM | 481 | 474 | 417 | 1 784 |

- `/ping` (pure framework) — Ferrox **×2.5** over Litestar, **×3.8** over FastAPI.
- The `rawmodel` layer runs ≈ on par with plain msgspec — the per-request cost
  of the model layer is negligible.

## The ceiling: pure Granian

With **no framework at all**, the same server delivers:

```
granian, empty body     ~168 000 req/s
granian, {"ok": true}   ~145 000 req/s
```

So Ferrox's `/ping` (~91k) keeps **~63%** of the raw server's JSON ceiling;
Litestar ~25%, FastAPI ~17%, Django ~1%.

## Where the time goes (SELECT 100 rows, SQLite)

| Layer | ops/s | Loss |
|---|---|---|
| sqlite3 (sync C driver) | 33 506 | — (DB ceiling) |
| aiosqlite (async) | 15 080 | −55% |
| SQLAlchemy async ORM | 2 036 | −87% |
| Ferrox GET on Granian | 1 644 | −19% |

The ORM mapping is the real bottleneck (~88% of request time) — Ferrox adds only
~19% on top of it.

## Reproducing

```bash
./bench/run_all_ab.sh                              # full routing/payload matrix
.venv/bin/python bench/ab_bench.py --server granian --routes 1000 --requests 20000
.venv/bin/python bench/bench_db.py granian         # SQLite GET/POST
.venv/bin/python bench/bench_postgres.py granian   # PostgreSQL Core/ORM vs FastAPI
.venv/bin/python bench/bench_rows.py               # rows matrix (8 variants)
.venv/bin/python bench/bench_granian_bare.py       # pure granian ceiling
.venv/bin/python bench/plot_rows.py                # → bench_rows.html
```

Requires `ab` (apache2-utils) and, for the DB benches, PostgreSQL on `:5432`
(`docker run -d --name ferrox-pg -e POSTGRES_PASSWORD=postgres -e
POSTGRES_USER=postgres -e POSTGRES_DB=postgres -p 5432:5432 postgres:latest`).
