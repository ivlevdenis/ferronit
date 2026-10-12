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
| Ferronit + rawmodel (lifespan+DI) | 13 783 | 8 752 | 2 127 | 90 720 |
| BlackSheep + asyncpg | 15 359 | 9 215 | 2 154 | 88 546 |
| BlackSheep + SQLAlchemy Core | 3 387 | 1 886 | 376 | 88 548 |
| Litestar + msgspec | 10 985 | 7 862 | 2 062 | 36 772 |
| Litestar + asyncpg | 10 781 | 6 939 | 1 538 | 36 611 |
| Litestar + SQLAlchemy Core | 3 076 | 1 866 | 418 | 36 357 |
| Litestar + ORM | 2 616 | 1 415 | 289 | 35 028 |
| FastAPI + ORM | 2 228 | 922 | 147 | 19 784 |
| Django + ORM | 518 | 523 | 520 | 1 772 |

- `/ping` — Ferronit и BlackSheep вровень (~88–90k), **×2.5** к Litestar, **×4.6** к FastAPI.
- **SQLAlchemy Core в ~4–5 раз медленнее rawmodel** на чтении (3.1–3.4k против 13.8k на 1 строке,
  418 против 2 127 на 1000) — это цена `select()` + `mappings()` + `dict(r)`.
- Абсолютные req/s пляшут ±10–20% между сессиями — сравнивать соотношения внутри прогона.

## Записи: INSERT / UPDATE / UPSERT

Тот же стенд, батч-операции на 1 / 100 / 1000 строк (`ab -c 50 -k`, req/s):

| Операция | Ferronit rawmodel | BlackSheep asyncpg | BlackSheep Core | Litestar Core |
|---|---|---|---|---|
| INSERT | 5 153 / 3 264 / 522 | 8 504 / 4 567 / 753 | 2 366 / 1 640 / 387 | 2 379 / 1 546 / 365 |
| UPDATE | 878 / 304 / 32 | 1 043 / 264 / 31 | 825 / 282 / 33 | 811 / 278 / 33 |
| UPSERT | 903 / 398 / 35 | 1 038 / 421 / 45 | 806 / 218 / 27 | 816 / 219 / 26 |

- На записи Ferronit **на ~20–25% медленнее** BlackSheep-asyncpg — это цена явной транзакции UoW
  (`BEGIN`/`COMMIT`, 2 лишних round-trip ≈ 60 мкс), дающей атомарность и rollback.
  BlackSheep/Litestar пишут авто-коммитом — без отката. На INSERT rawmodel в ~2 раза быстрее
  SQLAlchemy Core; на UPDATE/UPSERT все упираются в fsync и сходятся.

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
