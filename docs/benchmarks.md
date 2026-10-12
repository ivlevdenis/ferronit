# Бенчмарки

Методика фиксирована: **только ApacheBench** (`ab -n N -c 50 -k`). Python-клиенты
(httpx/asyncio) упираются в ~3.7k req/s независимо от сервера, то есть меряют
клиент, а не фреймворк. Все приложения — на одном сервере, 1 воркер, лучший из
нескольких прогонов, keep-alive. Полный стенд: каталог `bench/`.

**Основной конкурент — BlackSheep** (ближайший соперник по маршрутизации);
Litestar/FastAPI — дополнительные точки отсчёта.

## Железо и окружение

| Параметр | Значение |
|---|---|
| CPU | Intel Core i7-12700H (12th Gen), 14 ядер / 20 потоков, до 4.7 ГГц |
| RAM | 32 ГБ |
| ОС | Ubuntu 26.04 LTS, ядро 7.0.0-generic |
| Диск | NVMe Samsung 970 EVO Plus 1 ТБ |
| Сервер | granian 2.8.4 / uvicorn, 1 воркер |
| Клиент | ApacheBench 2.3 |
| PostgreSQL | postgres:18 в Docker, localhost:5432 |

Цифры сняты `2026-10-12`, ollama остановлен (фоновый шум — hiddify ~10% CPU,
агент ~9%, load ~0.4). БД-варианты меряются **по очереди** (один сервер за раз) —
при одновременном запуске всех серверов они конкурируют за CPU и соединения БД,
цифры искажаются. Абсолюты шумят ±2–3% между сессиями, сравнивать стоит
соотношения внутри одного прогона.

## Маршрутизация

| Сценарий | Ferronit | BlackSheep | Litestar | FastAPI |
|---|---|---|---|---|
| uvicorn, 50 маршрутов | 20 979 | 19 955 | 14 329 | 5 632 |
| uvicorn, 1000 маршрутов | 20 181 | 19 101 | 13 970 | 2 611 |
| granian, 50 маршрутов | 101 165 | 92 115 | 38 090 | 10 070 |
| granian, 1000 маршрутов | 97 365 | 89 867 | 37 694 | 3 133 |

- Ferronit, BlackSheep и Litestar **не деградируют** с ростом таблицы маршрутов;
  FastAPI проседает до 6× внутри одной таблицы (11.8k → 3.1k → 1.8k на granian/1000).
- На granian Ferronit быстрее BlackSheep в ~1.1×, Litestar в ~2.6×, FastAPI в ~31×.
- Granian убирает протокольные накладные uvicorn (Ferronit ×4.8).

## Размер ответа (payload)

| Сценарий | Ferronit | BlackSheep | Litestar | FastAPI |
|---|---|---|---|---|
| small (1 объект), uvicorn | 20 049 | 19 208 | 13 753 | 5 363 |
| medium (50), uvicorn | 18 759 | 14 417 | 13 130 | 2 843 |
| big (500), uvicorn | 12 733 | 4 953 | 9 706 | 602 |
| big (500), granian | 29 385 | 6 513 | 19 794 | 622 |

- На JSON-тяжёлых ответах (500 объектов) Ferronit быстрее BlackSheep в ×2.6
  (uvicorn) и ×4.5 (granian) — msgspec C-энкодер против тяжёлого пайплайна
  BlackSheep. FastAPI отстаёт в ×21–47.

## Строки из БД: 1 / 100 / 1000

`GET /rows`, granian, 1 воркер, `ab -c 50 -k`, req/s:

| Вариант | 1 строка | 100 строк | 1000 строк | /ping |
|---|---|---|---|---|
| Ferronit rawmodel | 33 196 | 32 947 | 28 480 | 185 360 |
| BlackSheep (asyncpg+msgspec) | 15 396 | 9 563 | 2 268 | 91 616 |
| Litestar msgspec | 10 894 | 7 708 | 2 247 | 38 166 |
| Litestar asyncpg | 10 094 | 6 410 | 1 610 | 37 173 |
| BlackSheep SQLAlchemy Core | 3 480 | 1 904 | 405 | 91 613 |
| Litestar SQLAlchemy Core | 3 312 | 2 028 | 450 | 38 310 |
| Litestar ORM | 2 718 | 1 439 | 301 | 37 545 |
| FastAPI ORM | 2 360 | 966 | 157 | 20 403 |
| Django ORM | 583 | 585 | 584 | 1 911 |

- Ferronit почти не проседает с ростом числа строк (33.2k → 28.5k) — узкое место
  в общей части (asyncpg + msgspec), а не в фреймворке; потолок одного процесса
  asyncpg ~33k req/s (см. `bench/README.md`).
- BlackSheep на 1000 строк отстаёт в ×12.6; SQLAlchemy Core в ~4–5× медленнее
  rawmodel; Django упирается в threadpool.

## Записи: INSERT / UPDATE / UPSERT

Батч-операции на 1 / 100 / 1000 строк, `ab -c 50 -k`, req/s:

| Операция | Ferronit | BlackSheep | Litestar msgspec | BlackSheep Core | Litestar Core |
|---|---|---|---|---|---|
| INSERT | 14 939 / 4 657 / 717 | 8 366 / 5 144 / 837 | 7 386 / 4 713 / 795 | 2 696 / 1 710 / 419 | 2 523 / 1 826 / 436 |
| UPDATE | 876 / 274 / 34 | 1 063 / 352 / 36 | 1 070 / 262 / 36 | 862 / 225 / 35 | 867 / 218 / 35 |
| UPSERT | 883 / 343 / 41 | 1 082 / 428 / 43 | 1 084 / 347 / 36 | 861 / 246 / 30 | 479 / 232 / 27 |

- На INSERT Ferronit впереди (×1.8 на одиночной строке — bulk-вставка + RETURNING);
  на UPDATE/UPSERT Ferronit на ~15–20% медленнее конкурентов — цена явной
  транзакции UoW (`BEGIN`/`COMMIT`, ~60 мкс) ради атомарности и rollback.
  SQLAlchemy Core на INSERT в ~5–6× медленнее rawmodel.

## Связи: GET /parents (N родителей × 3 ребёнка)

Ferronit грузит связи через `Relation` (2 запроса + позиционный маппинг + msgspec),
конкуренты — вручную. `ab -c 50 -k`, req/s:

| Родителей | Ferronit | BlackSheep | Litestar msgspec |
|---|---|---|---|
| 1 | 16 930 | 8 741 | 7 547 |
| 100 | 15 732 | 2 079 | 2 556 |
| 1000 | 16 522 | 272 | 381 |

- Разрыв растёт с числом родителей: ×1.9 на 1, ×7.6 на 100, ×61 на 1000 —
  msgspec C-сериализация + позиционный маппинг против dict-обхода конкурентов.

## Потолок: чистый Granian

Без фреймворка тот же сервер выдаёт ~120 000–185 000 req/s (границы замера,
цифры шумят ±20%). Ferronit `/ping` в матрицах выше — 101–185k; строгие проценты
к потолку не выводим — абсолюты между сессиями несравнимы.

## Воспроизведение

```bash
./bench/run_all_ab.sh                              # маршрутизация + payload (4 фреймворка)
.venv/bin/python bench/bench_rows.py               # строки + записи + связи (по очереди)
.venv/bin/python bench/bench_granian_bare.py       # потолок чистого granian
```

Требуется `ab` (apache2-utils), granian/uvicorn и PostgreSQL на `:5432`
(`docker run -d --name ferronit-pg -e POSTGRES_PASSWORD=postgres -e
POSTGRES_USER=postgres -e POSTGRES_DB=postgres -p 5432:5432 postgres:latest`).
Перед прогоном остановить ollama и прочих фоновых потребителей CPU.
