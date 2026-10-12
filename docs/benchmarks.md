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

## Строки: Ferronit против Litestar/FastAPI/Django/BlackSheep

Тот же PostgreSQL, granian, 1 воркер, `ab -c 50 -k` (1 / 100 / 1000 строк):

| Вариант | 1 строка | 100 строк | 1000 строк | /ping |
|---|---|---|---|---|
| Ferronit + rawmodel (lifespan+DI) | 11 795 | 8 121 | 2 033 | 87 246 |
| BlackSheep + msgspec | 13 598 | 9 154 | 2 129 | 85 529 |
| Litestar + msgspec | 11 059 | 7 671 | 1 957 | 36 135 |
| Litestar + asyncpg | 10 936 | 6 228 | 1 413 | 35 689 |
| Litestar + ORM | 2 525 | 1 348 | 279 | 35 935 |
| FastAPI + ORM | 2 240 | 895 | 142 | 19 583 |
| Django + ORM | 510 | 520 | 537 | 1 770 |

- `/ping` — Ferronit и BlackSheep вровень (~85–87k), **×2.4** к Litestar, **×4.5** к FastAPI.
- Абсолютные req/s пляшут ±10–20% между сессиями — сравнивать соотношения внутри прогона.

## Записи: INSERT / UPDATE / UPSERT

Тот же стенд, батч-операции на 1 / 100 / 1000 строк (`ab -c 50 -k`, req/s):

| Операция | Ferronit rawmodel | BlackSheep | Litestar msgspec |
|---|---|---|---|
| INSERT | 5 811 / 3 078 / 514 | 7 533 / 4 589 / 619 | 6 885 / 3 880 / 631 |
| UPDATE | 867 / 225 / 33 | 1 040 / 259 / 31 | 1 063 / 309 / 32 |
| UPSERT | 897 / 211 / 36 | 559 / 229 / 35 | 385 / 214 / 34 |

- На записи Ferronit **на ~20–25% медленнее** BlackSheep — это цена явной транзакции UoW
  (`BEGIN`/`COMMIT`, 2 лишних round-trip ≈ 60 мкс), дающей атомарность и rollback.
  BlackSheep/Litestar пишут авто-коммитом — без отката. На чтении разницы нет.

## Потолок: чистый Granian

Совсем **без фреймворка** тот же сервер выдаёт:

```
Совсем **без фреймворка** тот же сервер выдаёт (границы замера, цифры шумят ±20%):

granian, пустое тело     ~120 000–185 000 req/s
granian, {"ok": true}    ~135 000–170 000 req/s
```

`/ping` у Ferronit из матрицы выше — 128 192 req/s; строгие проценты к потолку
не выводим — абсолюты между сессиями несравнимы.

## Воспроизведение

```bash
./bench/run_all_ab.sh                              # вся матрица маршрутизации/payload
.venv/bin/python bench/ab_bench.py --server granian --routes 1000 --requests 20000
.venv/bin/python bench/bench_rows.py               # матрица строк (6 вариантов)
.venv/bin/python bench/bench_granian_bare.py       # потолок чистого granian
.venv/bin/python bench/plot_rows.py                # → bench_rows.html
```

Требуется `ab` (apache2-utils) и, для БД-бенчей, PostgreSQL на `:5432`
(`docker run -d --name ferronit-pg -e POSTGRES_PASSWORD=postgres -e
POSTGRES_USER=postgres -e POSTGRES_DB=postgres -p 5432:5432 postgres:latest`).
