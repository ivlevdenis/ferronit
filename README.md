# Ferrox

High-performance Python ASGI web framework with a Rust core.

**Documentation** (EN / RU): [`docs/`](docs/README.md) — полный справочник по
фреймворку: роутинг, запросы, инъекция, БД-слои, DDD/DI, Rust-ядро, бенчи.

## Installation

```bash
pip install ferrox        # ставит сразу Python-слой и нативное ядро (ferrox._core + ferrox.db)
```

Ferrox — **один пакет**, собранный [maturin](https://www.maturin.rs)-ом как mixed-проект:

- **`ferrox`** — Python-слой: ASGI-движок, DDD/CQRS, DI, contrib; внутри лежит нативное
  ядро `ferrox._core` (роутинг, парсинг запроса, JSON, gzip, CORS)
- **`ferrox.db`** — слой данных (PostgreSQL → JSON целиком в Rust, для read-heavy ручек
  с большими выборками): `connect` / `query_json`

Ядро собирается с фичей `abi3-py312`, поэтому один wheel (`cp312-abi3`) работает на всех
CPython от 3.12 до 3.14+ — матрица версий Python не нужна. Версия в `ferrox/__init__.py`
и `ferrox-rs/Cargo.toml` всегда совпадает (проверяется тестом `tests/test_packaging.py`).

### Из исходников

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e .   # dev-установка: maturin соберёт ferrox + ferrox._core
./scripts/build_packages.sh                     # один wheel в dist/
```

`pyproject.toml` — maturin mixed-проект с `manifest-path = "ferrox-rs/Cargo.toml"`, поэтому
`pip install -e .` не ходит на PyPI. Ядро отдельно (без переустановки ferrox):

```bash
PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 .venv/bin/maturin develop --release
```

## Docker

Образ самодостаточный: ядро компилируется maturin-ом внутри билд-стейджа, наружу идёт
`python:3.12-slim` с одним wheel-ом, прод-сервером Granian и непривилегированным
пользователем. Ни ABI, ни Rust на машине для запуска не нужны.

```bash
docker build -t ferrox:0.8.0 .
docker run --rm -p 8000:8000 ferrox:0.8.0
curl localhost:8000/          # {"service":"ferrox","status":"ok","version":"0.8.0",...}
```

Своё приложение — монтированием каталога и переменной `APP`:

```bash
docker run --rm -p 8000:8000 -v "$PWD:/app" -e APP=app:app ferrox:0.8.0
```

`docker compose` поднимает то же самое одной командой:

```bash
docker compose up --build api            # минимальное приложение, :8000
docker compose up --build ecommerce      # DDD-пример на SQLite, :8001
docker compose --profile postgres up -d  # Postgres 18 рядом, :5432
```

Переменные контейнера: `APP` (модуль:атрибут, по умолчанию `demo_app:app`), `SERVER`
(`granian` | `uvicorn`), `HOST`, `PORT`, `WORKERS`, `RELOAD=1` (dev-режим с авто-перезагрузкой).
`HEALTHCHECK` проверяет живое приложение по HTTP, а не только открытый порт.

## Benchmarks

Метод — **только ApacheBench** (`ab -n N -c 50 -k`, C-клиент). Python-клиент (httpx/asyncio)
сам упирается в ~3.5–3.7k req/s независимо от сервера, поэтому httpx-замеры мерят клиент, а не
фреймворк; такие скрипты убраны в `bench/legacy_httpx/`. Оба приложения — одинаковый код на
одинаковом сервере, 1 воркер, медиана из 3 прогонов, маршруты сэмплируются (первый / средний /
последний), keep-alive. Полный прогон одной командой: `./bench/run_all_ab.sh` →
`bench/RESULTS_ab.txt` (замеры 2026-10-04, машина простаивала: load 0.33 на 20 ядрах).

| Scenario | Ferrox | FastAPI | Gap |
|---|---|---|---|
| uvicorn, 50 routes | 20 060 req/s | 6 589 req/s | **×3.04** |
| uvicorn, 1000 routes | 20 216 req/s | 2 817 req/s | **×7.18** |
| granian, 50 routes | **96 298 req/s** | 12 135 req/s | **×7.94** |
| granian, 1000 routes | **94 873 req/s** | 3 416 req/s | **×27.8** |
| uvicorn, payload 500 objects | 1 915 req/s | 639 req/s | **×3.00** |
| granian, payload 500 objects | 2 130 req/s | 647 req/s | **×3.29** |

Что из этого следует:

- **Ferrox не деградирует с ростом таблицы маршрутов**: 20 060 → 20 216 req/s (uvicorn) и
  96 298 → 94 873 (granian) при переходе с 50 на 1000 маршрутов. Внутри одного приложения
  первый, средний и последний маршруты дают одинаковые числа (uvicorn 1000: 20 216 / 20 047 / 20 498).
- **FastAPI деградирует вдоль таблицы маршрутов**: на 1000 маршрутах `/route0` — 7 069 req/s,
  `/route500` — 2 817, `/route999` — 1 734 (падение в 4 раза внутри одного процесса). На границе
  таблицы разрыв доходит до ×27.8 (granian: 94 873 против 1 971).
- **Granian убирает накладные расходы протокола uvicorn**: Ferrox ×4.8 (20 060 → 96 298),
  FastAPI ×1.8 (6 589 → 12 135) — Rust-сервер выгоднее тому, у кого тоньше Python-слой.
- **Задержки**: Ferrox p99 = 1 мс на granian и 3 мс на uvicorn; FastAPI на 1000 маршрутах
  доходит до p99 = 46 мс.
- 96 298 req/s ≈ 10.4 µs на запрос — потолок Python-хендлера, а не сервера.

**Цена abi3.** Ядро собирается с `abi3-py312` (один wheel на все Python ≥ 3.12) — проверено, что это
почти бесплатно: granian, 1000 маршрутов, ab -n 30000 -c 50 -k, медиана из 5 прогонов —
**abi3 94 762 req/s vs нативное ядро 95 773 req/s (−1.4 %)**, при разбросе между прогонами ±10 %.
Экономия на матрице сборок стоит ~1 % throughput.

### With database (SQLite, handler → ORM/Core → response, ab)

| Server | Operation | Ferrox ORM | Ferrox Core | FastAPI |
|---|---|---|---|---|
| Granian | GET (SELECT 100 rows) | 1 616 req/s | **2 362** (+46%) | 1 198 (**+97%**) |
| Granian | POST (INSERT) | 4 012 req/s | **4 961** (+24%) | 3 143 (**+58%**) |
| uvicorn | GET (SELECT 100 rows) | 1 371 req/s | — | 1 063 (+29%) |
| uvicorn | POST (INSERT) | 2 560 req/s | — | 2 121 (+21%) |

The `CoreRepository` (SQLAlchemy Core, dicts instead of ORM objects) adds +46% on reads and +24% on writes; Ferrox+Core beats FastAPI by ~2× on reads. The DB becomes the shared bottleneck, so the gap narrows — but Ferrox stays ahead on both reads and writes, and Granian still adds ~20% on top of uvicorn.

### SQLite scaling (what actually works)

Raw ceilings: sync `sqlite3` SELECT 100 rows — 33.5k ops/s, `aiosqlite` — 15.5k. Through HTTP (Granian, 50 concurrent, 1 worker):

| Approach | req/s |
|---|---|
| SQLAlchemy Core (session per request) | 2 329 |
| raw aiosqlite, single connection | 4 345 |
| connection pool (8 conns) | 1 009 ⚠️ |
| sync sqlite3 + `to_thread` | 873 ⚠️ |
| **raw aiosqlite + 4 workers** | **14 016** |

A single connection serializes concurrent requests; a connection pool makes it *worse* (each aiosqlite connection is a thread — GIL contention). **The working pattern: one aiosqlite connection per process + `ferrox run --workers N`** — scales almost linearly (4 workers = 90% of the aiosqlite ceiling).

### PostgreSQL 18 (Docker, default settings, Granian, ab)

Requires: `docker run -d --name ferrox-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_USER=postgres -e POSTGRES_DB=postgres -p 5432:5432 postgres:latest`

| Operation | Ferrox Core | Ferrox ORM | FastAPI |
|---|---|---|---|
| GET (SELECT 100 rows, LIMIT) | 1 072 req/s | 738 (+45%) | 596 (**+80%**) |
| POST (INSERT + commit) | 639 req/s | 616 | 568 (+13%) |

POST is disk-bound (fsync in Docker ~1.6 ms/commit); GET shows Ferrox+Core at +80% over FastAPI. `INSERT...RETURNING` is native on Postgres (no slowdown, unlike SQLite).

### Ferrox vs Litestar и FastAPI (1/100/1000 строк)

Тот же PostgreSQL, один сервер для всех (granian, 1 воркер), `ab -c 50 -k`, лучший из 2.
Полная матрица из восьми вариантов — `bench/bench_rows.py`; график — `bench/plot_rows.py`
(→ `bench_rows.html`). req/s:

| вариант | 1 строка | 100 строк | 1000 строк | /ping |
|---|---|---|---|---|
| Ferrox + asyncpg (dict) | **15 540** | 7 849 | 1 275 | 91 935 |
| Ferrox + msgspec | 14 611 | **10 664** | **2 279** | 91 642 |
| Ferrox + rawmodel | 13 959 | 9 011 | 2 199 | 91 208 |
| Litestar + msgspec | 11 138 | 8 218 | 2 032 | 36 636 |
| Litestar + ORM | 2 474 | 1 028 | 283 | 37 503 |
| FastAPI + ORM | 2 072 | 680 | 161 | 24 408 |
| Django + ORM | 481 | 474 | 417 | 1 784 |

- **Ferrox быстрее Litestar во всём**: на `/ping` (чистый фреймворк) — **×2.5**; на строках —
  ×1.1–1.3, потому что там доминирует общая часть (PostgreSQL + asyncpg + msgspec), а не роутер.
  FastAPI — ×3.8 на `/ping` и ×10–14 на строках (сверху SQLAlchemy ORM).
- **`rawmodel` (типизированные модели + SQL-DSL) идёт ≈ вровень с `msgspec`** — 2-кратной
  платы за модель больше нет (перевод на `msgspec.Struct`, read-only транзакции, без
  identity-map на `list`).
- Django — sync-view под ASGI: threadpool-обвязка режет `/ping` до 1 784 req/s.

### Where the time goes (SELECT 100 rows)

| Layer | ops/s | Loss |
|---|---|---|
| sqlite3 (sync C driver) | 33 506 | — (DB ceiling) |
| aiosqlite (async) | 15 080 | −55% |
| SQLAlchemy async ORM | 2 036 | **−87%** |
| Ferrox GET on Granian | 1 644 | −19% |

The ORM mapping is the real bottleneck (~88% of request time), not the framework — Ferrox adds only ~19% on top of the ORM layer.

### Payload size (uvicorn, ab, 50 concurrent, median of 3 runs)

| Response size | Ferrox | FastAPI | Gap |
|---|---|---|---|
| small (`{"ok":true}`) | 19 991 req/s | 6 113 req/s | ×3.27 |
| medium (50 objects) | 10 089 req/s | 3 170 req/s | ×3.18 |
| big (500 objects) | 1 915 req/s | 639 req/s | ×3.00 |

С ростом ответа обе библиотеки теряют throughput на сериализации (Ferrox 20.0k → 1.9k, FastAPI 6.1k →
0.6k), но **разрыв остаётся стабильным ×3**: Rust-сериализация не даёт Ferrox «сломаться» на крупных
ответах. Прежние httpx-числа (546 против 193 req/s, «+181 %») были client-bound — с `ab` реальный
разрыв снова ×3.

**Key takeaway:** маршрутизация не деградирует с числом маршрутов — Rust `matchit` фактически O(1):
Ferrox держит 20k/95k req/s при 50 и 1000 маршрутах, FastAPI теряет до 4× внутри одной таблицы.
Замеры через httpx вводили в заблуждение (потолок клиента ~3.7k req/s) — источник цифр теперь только `ab`.

### Security (production hardening)

```python
from ferrox import Ferrox
from ferrox.contrib.security import security_headers
from ferrox.contrib.ratelimit import rate_limit

app = Ferrox(max_body_size=10 * 1024 * 1024)   # 413 for oversized bodies
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

## Development

```bash
./scripts/check.sh                      # линт → типы → докстринги → сборка Rust-ядра → тесты
.venv/bin/python scripts/agent_readiness.py   # покрытие публичного API докстрингами (100%)
```

- **AGENTS.md** — инструкция для код-агентов (Claude Code, Codex, Cursor, Copilot): структура,
  команды, инварианты, стиль, границы работ. `CLAUDE.md`, `.cursor/rules/ferrox.mdc` и
  `.github/copilot-instructions.md` ссылаются на него.
- **`examples/`** — по одному примеру на кейс: minimal, DI, LLM + SSE, RAG, WebSocket (все под
  тестами `tests/test_examples.py`); `examples/app.py` — полный DDD-пример.
- Пакет помечен `py.typed`; для Rust-ядра лежит `ferrox/_core.pyi`, поэтому mypy и IDE
  видят типы ядра. Линт — ruff, типы — mypy (обе команды в `check.sh`).
- Всего тестов: **217**; из них `tests/security/` — 64 (в т.ч. 14 ASVS L1).

### Run

```bash
ferrox dev                          # dev server (uvicorn, auto-reload)
ferrox dev --server granian         # dev on Granian (Rust, ~4x faster)
ferrox run                          # production (Granian by default, falls back to uvicorn)
ferrox run --workers 4              # scale with worker processes
```

### Reproduce

```bash
./bench/run_all_ab.sh                              # вся матрица (routing/payload), пишет bench/RESULTS_ab.txt
.venv/bin/python bench/ab_bench.py --server granian --routes 1000 --requests 20000
.venv/bin/python bench/ab_bench.py --server uvicorn --payload big --requests 3000
.venv/bin/python bench/bench_db.py granian         # DB benchmark (SQLite, GET/POST, ab)
.venv/bin/python bench/bench_postgres.py granian   # PostgreSQL 18 (Core/ORM vs FastAPI, ab)
.venv/bin/python bench/bench_rows.py               # Ferrox vs Litestar/FastAPI/Django (1/100/1000 строк)
.venv/bin/python bench/plot_rows.py               # график → bench_rows.html (SVG, тёмная тема)
.venv/bin/python bench/bench_rustdb.py             # ferrox.db (запросы в Rust) против asyncpg
```

Стенд целиком вынесен в `bench/` (см. `bench/README.md`): это черновой измерительный код, он
не линтуется, не входит в sdist и Docker-образ. Helper-приложения (`bench/_bench_*.py`) запускаются
основными скриптами динамически — не удаляй их, не проверив `bench/bench_*.py`.

Machine: local dev box, Python 3.14, single-core uvicorn worker.
