# Бенчмарки

Методика фиксирована: **только ApacheBench** (`ab -n N -c 50 -k`). Python-клиенты
(httpx/asyncio) упираются в ~3.7k req/s независимо от сервера — меряют клиент, а не
фреймворк. Все приложения — на одном сервере, 1 воркер, лучший из 2 прогонов, keep-alive.
Полный стенд: `bench/api_bench.py`.

**Основной конкурент — BlackSheep** (ближайший соперник по маршрутизации);
Litestar/FastAPI/Django — дополнительные точки отсчёта.

## Железо и окружение

| Параметр | Значение |
|---|---|
| CPU | Intel Core i7-12700H (12th Gen), 14 ядер / 20 потоков, до 4.7 ГГц |
| RAM | 32 ГБ |
| ОС | Ubuntu 26.04 LTS, ядро 7.0.0-generic |
| Сервер | granian 2.8.4, 1 воркер |
| Клиент | ApacheBench 2.3 |
| PostgreSQL | postgres:18 в Docker, localhost:5432 |

Цифры сняты `2026-10-12`. Абсолюты шумят ±2–3% между сессиями, сравнивать стоит
соотношения внутри одного прогона.

## GET /ping — простой dict

`return {"ok": true}` на каждом фреймворке, granian, 1 воркер, `ab -c 50 -k`, req/s:

| Ferronit | BlackSheep | Litestar | FastAPI | Django |
|---|---|---|---|---|
| **98 197** | 93 523 | 37 719 | 19 036 | 2 033 |

Ferronit на чистом роутинге быстрее BlackSheep в ~1.05×, Litestar в ~2.6×, FastAPI в ~5×;
Django (sync-view под ASGI) упирается в threadpool (~2k).

## Строки из БД: GET /rows?limit=N

`SELECT id, name, email … LIMIT $1`, где `N` (1/20/50/100/1000) приходит **query-параметром**,
как в реальном API. `ab -c 50 -k`, granian, 1 воркер, req/s:

| limit | Ferronit | BlackSheep | Litestar | FastAPI | Django |
|---|---|---|---|---|---|
| 1 | 13 524 | **14 228** | 9 736 | 2 127 | 600 |
| 20 | 12 597 | **13 298** | 9 873 | 1 692 | 595 |
| 50 | 9 584 | **9 809** | 8 469 | 1 304 | 603 |
| 100 | **8 350** | 8 065 | 6 727 | 956 | 602 |
| 1000 | **2 334** | 1 780 | 1 704 | 158 | 593 |

- На малых лимитах (1–50 строк) BlackSheep впереди — сырой asyncpg без слоя модели;
  на 100–1000 Ferronit впереди за счёт позиционного маппинга rawmodel (`cls(*row)`) + msgspec.
- FastAPI (SQLAlchemy ORM) отстаёт в ×4–15, Django (sync ORM) — на ~600 req/s.

## Одиночные записи

`ab -c 50 -k`, granian, 1 воркер, req/s. Перед каждой операцией таблица восстанавливается
(`TRUNCATE` + 1000 строк), чтобы UPDATE/UPSERT/DELETE били по существующим строкам:

| Операция | Ferronit | BlackSheep | Litestar | FastAPI | Django |
|---|---|---|---|---|---|
| INSERT | 5 300 | **7 850** | 6 869 | 1 132 | 479 |
| UPDATE | 946 | 1 070 | 1 077 | **1 519** | 458 |
| UPSERT | 958 | **1 089** | 1 094 | 706 | 638 |
| DELETE | 8 956 | **16 972** | 10 056 | 2 081 | 595 |

- INSERT у всех упирается в fsync/коммит (~5–8k); UPDATE/UPSERT — в явную транзакцию
  (`BEGIN`/`COMMIT`) — у Ferronit ~950, у FastAPI на UPDATE выше за счёт сессионного кэша.
- DELETE быстрее INSERT (нет вставки данных) — но это **верхняя граница**: после первого
  удаления `id=1` остальные запросы — no-op (ab не меняет id динамически).

## Воспроизведение

```bash
./bench/run_all_ab.sh                      # ping + rows?limit + записи, пишет bench/RESULTS_ab.txt
.venv/bin/python bench/api_bench.py        # то же без файла
BENCH_APPS=ferronit,blacksheep .venv/bin/python bench/api_bench.py   # выборочно
```

Требуется `ab` (apache2-utils), granian и PostgreSQL на `:5432`
(`docker run -d --name ferronit-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_USER=postgres
-e POSTGRES_DB=postgres -p 5432:5432 postgres:latest`). Перед прогоном остановить ollama
и прочих фоновых потребителей CPU.

Старые сценарии (маршрутизация 50/1000, размер ответа, связи, потолок granian) — в
`bench/legacy/`.
