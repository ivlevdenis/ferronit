# Бенчмарки

Методика фиксирована: **только ApacheBench** (`ab -n N -c 50 -k`). Python-клиенты
(httpx/asyncio) упираются в ~3.7k req/s независимо от сервера, то есть меряют
клиент, а не фреймворк. Все приложения — на одном сервере, 1 воркер, лучший из
нескольких прогонов, keep-alive. Полный стенд: каталог `bench/`.

## Маршрутизация: Ferrox против FastAPI

| Сценарий | Ferrox | FastAPI | Разрыв |
|---|---|---|---|
| uvicorn, 50 маршрутов | 20 060 | 6 589 | ×3.0 |
| uvicorn, 1000 маршрутов | 20 216 | 2 817 | ×7.2 |
| granian, 50 маршрутов | 96 298 | 12 135 | ×7.9 |
| granian, 1000 маршрутов | 94 873 | 3 416 | ×27.8 |

- Ferrox **не деградирует** с ростом таблицы маршрутов (20 060 → 20 216 на
  uvicorn; 96 298 → 94 873 на granian при переходе 50 → 1000). FastAPI
  замедляется до 4× *внутри одной* таблицы маршрутов.
- Granian убирает протокольные накладные uvicorn: Ferrox ×4.8, FastAPI ×1.8.

## Строки: Ferrox против Litestar/FastAPI/Django

Тот же PostgreSQL, granian, 1 воркер, `ab -c 50 -k` (1 / 100 / 1000 строк):

| Вариант | 1 строка | 100 строк | 1000 строк | /ping |
|---|---|---|---|---|
| Ferrox + asyncpg (dict) | 15 540 | 7 849 | 1 275 | 91 935 |
| Ferrox + msgspec | 14 611 | 10 664 | 2 279 | 91 642 |
| Ferrox + rawmodel | 13 959 | 9 011 | 2 199 | 91 208 |
| Litestar + msgspec | 11 138 | 8 218 | 2 032 | 36 636 |
| Litestar + ORM | 2 474 | 1 028 | 283 | 37 503 |
| FastAPI + ORM | 2 072 | 680 | 161 | 24 408 |
| Django + ORM | 481 | 474 | 417 | 1 784 |

- `/ping` (чистый фреймворк) — Ferrox **×2.5** к Litestar, **×3.8** к FastAPI.
- Слой `rawmodel` идёт ≈ вровень с обычным msgspec — per-request цена модели
  пренебрежима.

## Потолок: чистый Granian

Совсем **без фреймворка** тот же сервер выдаёт:

```
granian, пустое тело     ~168 000 req/s
granian, {"ok": true}    ~145 000 req/s
```

То есть `/ping` у Ferrox (~91k) сохраняет **~63%** от JSON-потолка сырого
сервера; Litestar ~25%, FastAPI ~17%, Django ~1%.

## Куда уходит время (SELECT 100 строк, SQLite)

| Слой | ops/s | Потери |
|---|---|---|
| sqlite3 (синхронный C-драйвер) | 33 506 | — (потолок БД) |
| aiosqlite (async) | 15 080 | −55% |
| SQLAlchemy async ORM | 2 036 | −87% |
| Ferrox GET на Granian | 1 644 | −19% |

Узкое место — ORM-mapping (~88% времени запроса) — Ferrox добавляет лишь ~19%
поверх ORM-слоя.

## Воспроизведение

```bash
./bench/run_all_ab.sh                              # вся матрица маршрутизации/payload
.venv/bin/python bench/ab_bench.py --server granian --routes 1000 --requests 20000
.venv/bin/python bench/bench_db.py granian         # SQLite GET/POST
.venv/bin/python bench/bench_postgres.py granian   # PostgreSQL Core/ORM против FastAPI
.venv/bin/python bench/bench_rows.py               # матрица строк (8 вариантов)
.venv/bin/python bench/bench_granian_bare.py       # потолок чистого granian
.venv/bin/python bench/plot_rows.py                # → bench_rows.html
```

Требуется `ab` (apache2-utils) и, для БД-бенчей, PostgreSQL на `:5432`
(`docker run -d --name ferrox-pg -e POSTGRES_PASSWORD=postgres -e
POSTGRES_USER=postgres -e POSTGRES_DB=postgres -p 5432:5432 postgres:latest`).
