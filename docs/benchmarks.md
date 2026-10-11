# Бенчмарки

Методика фиксирована: **только ApacheBench** (`ab -n N -c 50 -k`). Python-клиенты
(httpx/asyncio) упираются в ~3.7k req/s независимо от сервера, то есть меряют
клиент, а не фреймворк. Все приложения — на одном сервере, 1 воркер, лучший из
нескольких прогонов, keep-alive. Полный стенд: каталог `bench/`.

## Маршрутизация: Ferronit против FastAPI

| Сценарий | Ferronit | FastAPI | Разрыв |
|---|---|---|---|
| uvicorn, 50 маршрутов | 20 060 | 6 589 | ×3.0 |
| uvicorn, 1000 маршрутов | 20 216 | 2 817 | ×7.2 |
| granian, 50 маршрутов | 96 298 | 12 135 | ×7.9 |
| granian, 1000 маршрутов | 94 873 | 3 416 | ×27.8 |

- Ferronit **не деградирует** с ростом таблицы маршрутов (20 060 → 20 216 на
  uvicorn; 96 298 → 94 873 на granian при переходе 50 → 1000). FastAPI
  замедляется до 4× *внутри одной* таблицы маршрутов.
- Granian убирает протокольные накладные uvicorn: Ferronit ×4.8, FastAPI ×1.8.

## Строки: Ferronit против Litestar/FastAPI/Django

Тот же PostgreSQL, granian, 1 воркер, `ab -c 50 -k` (1 / 100 / 1000 строк):

| Вариант | 1 строка | 100 строк | 1000 строк | /ping |
|---|---|---|---|---|
| Ferronit + asyncpg (dict) | 16 067 | 7 988 | 1 450 | 92 481 |
| Ferronit + msgspec | 16 617 | 11 309 | 2 473 | 92 036 |
| Ferronit + rawmodel | 15 080 | 10 138 | 2 368 | 94 118 |
| Litestar + msgspec | 11 797 | 8 429 | 2 304 | 37 692 |
| Litestar + asyncpg | 11 310 | 7 799 | 1 691 | 37 616 |
| Litestar + ORM | 2 495 | 1 462 | 293 | 36 796 |
| FastAPI + ORM | 2 236 | 1 020 | 158 | 20 107 |
| Django + ORM | 344 | 349 | 320 | 1 733 |

- `/ping` (чистый фреймворк) — Ferronit **×2.5** к Litestar, **×4.7** к FastAPI.
- Слой `rawmodel` идёт ≈ вровень с обычным msgspec — per-request цена модели
  пренебрежима.

## Потолок: чистый Granian

Совсем **без фреймворка** тот же сервер выдаёт:

```
granian, пустое тело     ~168 000 req/s
granian, {"ok": true}    ~145 000 req/s
```

То есть `/ping` у Ferronit (~94k) сохраняет **~65%** от JSON-потолка сырого
сервера; Litestar ~26%, FastAPI ~14%, Django ~1%.

## Воспроизведение

```bash
./bench/run_all_ab.sh                              # вся матрица маршрутизации/payload
.venv/bin/python bench/ab_bench.py --server granian --routes 1000 --requests 20000
.venv/bin/python bench/bench_rows.py               # матрица строк (8 вариантов)
.venv/bin/python bench/bench_granian_bare.py       # потолок чистого granian
.venv/bin/python bench/plot_rows.py                # → bench_rows.html
```

Требуется `ab` (apache2-utils) и, для БД-бенчей, PostgreSQL на `:5432`
(`docker run -d --name ferronit-pg -e POSTGRES_PASSWORD=postgres -e
POSTGRES_USER=postgres -e POSTGRES_DB=postgres -p 5432:5432 postgres:latest`).
