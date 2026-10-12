# Стенд замеров Ferronit

Здесь лежат скрипты, которыми получены цифры в README. Это **не часть пакета**: черновой
измерительный код, который не линтуется (`ruff` исключает `bench/`) и не попадает в
sdist/Docker-образ. Логика фреймворка от него не зависит.

**Основной конкурент для сравнения — BlackSheep.** Бенчи по умолчанию сравнивают Ferronit
с BlackSheep (ближайший соперник), остальные (FastAPI/Litestar/Django) — дополнительные точки.
TurboAPI (`turboapi` из PyPI) меряется как есть: у него собственный Rust-сервер (`app.run`),
на granian он не работает.

## Политика: req/s меряем только `ab`

**Единственный источник цифр по HTTP — ApacheBench (`ab`, C-клиент).** Python-клиент
(httpx/asyncio) сам упирается в ~3.5–3.7k req/s независимо от сервера: в таких замерах
измеряется клиент, а не фреймворк. Поэтому httpx-скрипты переехали в `legacy_httpx/`
и **не используются для сравнения**.

## Основное

```bash
./bench/run_all_ab.sh                     # вся матрица (granian), ~5–6 минут, пишет bench/RESULTS_ab.txt
.venv/bin/python bench/ab_bench.py --server granian --routes 50
.venv/bin/python bench/ab_bench.py --server granian --routes 1000 --requests 20000
.venv/bin/python bench/bench_rows.py               # Ferronit-rawmodel vs Litestar/FastAPI/Django: 1/100/1000 строк
.venv/bin/python bench/bench_granian_bare.py       # потолок чистого granian
.venv/bin/python bench/plot_rows.py                # → bench_rows.html
```

`ab_bench.py` сам генерирует одинаковые приложения (Ferronit, BlackSheep, Litestar, FastAPI,
TurboAPI) с N маршрутами, поднимает их granian'ом (1 воркер; TurboAPI —
свой Rust-сервер через `app.run`), греет и меряет по выборке маршрутов **первый / средний /
последний** — так видно, что FastAPI деградирует при росте таблицы маршрутов, а Ferronit и
TurboAPI нет. Флаги: `--routes`, `--requests`, `--concurrency`, `--runs`,
`--payload {none,small,medium,big}`, `--no-fastapi`, `--no-litestar`, `--no-blacksheep`,
`--no-turboapi`.

| Скрипт | Что мерит |
|---|---|
| `ab_bench.py` | маршрутизация и размер ответа, Ferronit vs BlackSheep/Litestar/FastAPI/TurboAPI — **на `ab`** |
| `bench_rows.py`, `plot_rows.py` | Ferronit-rawmodel vs Litestar/FastAPI/Django: 1/100/1000 строк из PostgreSQL — **на `ab`** |
| `bench_granian_bare.py` | потолок чистого granian (пустое тело / JSON) — **на `ab`** |
| `pg_wire.py`, `wire_bench.py` | самодельный клиент по проводу PostgreSQL (SCRAM, simple/extended, пайплайн) против asyncpg |
| `micro_bench_json.py`, `micro_bench_response.py`, `micro_bench_model.py` | микрозамеры (Rust JSON против json.dumps, сборка ответа json+gzip, маппинг строк) — без HTTP |
| `rust_pg_orm/` | Rust-стенд: `tokio-postgres`, `sqlx`, `SeaORM` |
| `go_pg_orm/` | Go-стенд: `pgx`, `sqlx`, `GORM`, `ent` |
| `prof_*.py` | профилирование горячего пути (cProfile) |

## Прямой замер PostgreSQL (окт 2026)

pgbench, инстанс `postgres:18.6` (дефолтный конфиг Docker, 20 ядер, NVMe, scale 10):

| Замер | Результат |
|---|---|
| pgbench SELECT-only, 1 клиент | 57 473 tps |
| pgbench SELECT-only, 50 клиентов / 4 потока | **360 730 tps** |
| pgbench TPC-B (чтение + запись, fsync on) | 3 849 tps |

### Сырой драйвер: pgx против asyncpg против tokio-postgres

Тот же `SELECT ... LIMIT 100`, 10 клиентов × 1000 запросов, один процесс/поток:

| Драйвер | req/s |
|---|---|
| Go pgx | **40 500** |
| Python asyncpg | 32 869 |
| Rust tokio-postgres | 25 500 |

* pgx — самый быстрый, но все три сходятся на **42–45 тыс.** — это потолок самого запроса
  на этом инстансе, а не свойство языка/драйвера.
* asyncpg в один процесс упирается в GIL: 4 процесса × 10 клиентов дают 85 491 суммарно
  (по 20–23 тыс. на процесс). Потолок одного Python-процесса ~33 тыс.

### Самодельный клиент по проводу против asyncpg (`pg_wire.py`, `wire_bench.py`)

Протокол PostgreSQL v3 реализован вручную (startup, SCRAM-SHA-256, simple/extended, пайплайн).
10 клиентов × 1000 запросов, один процесс:

| Запрос | wire | asyncpg | разрыв |
|---|---|---|---|
| `SELECT ... LIMIT 1` | 47 919 | 57 778 | −17 % |
| `SELECT ... LIMIT 100` | 5 682 | 29 576 | −81 % |
| `INSERT`, 10 клиентов | 4 138 | 4 243 | −3 % |

* **Цена разбора строк: ≈15,7 мкс/строка у своего клиента против ≈1,7 мкс у asyncpg — в 9 раз.**
  Драйвер покупает компилируемый разбор результата, а не сеть и не протокол.
* Отброшено как объяснение: prepared+binary, пайплайн, `TCP_NODELAY`. На записи разницы нет —
  запись упирается в диск, а парсить нечего.

## rawdb + rawmodel — канонический слой данных

`RawRepository` отдаёт `dict`, `RawModelRepository` маппит строку позиционно (`cls(*row)`) без
словаря. Микрозамер изолирует маппинг (`micro_bench_model.py`, 1000 строк, лучшее из 200,
µs на строку):

| выборка | dict (RawRepository) | модель (RawModelRepository) |
|---|---|---|
| users (3 поля) | 0.40–0.43 | **0.32** |
| wide (10 полей) | 0.80–0.87 | **0.60–0.61** |

* Словарь стоит ≈4,4 позиционного кортежа (0.17 против 0.04 µs); модель дешевле dict в
  **×1,85–1,9**.
* Identity-map убран с `list()`, чтения идут `readonly=True` (без `BEGIN/COMMIT`) — это и есть
  выигрыш rawmodel. Тесты — `tests/test_rawmodel.py`.

## HTTP-сравнение фреймворков (`bench_rows.py`)

Канонические цифры (строки из БД, записи, связи) — в [`docs/benchmarks.md`](../docs/benchmarks.md),
они датированы прогоном и не дублируются здесь, чтобы не было двух расходящихся источников.
Воспроизведение (`GET /rows`, 1 / 100 / 1000 строк, `ab -c 50 -k`, granian, 1 воркер, по очереди):

```bash
.venv/bin/python bench/bench_rows.py
.venv/bin/python bench/plot_rows.py   # → bench_rows.html
```

## Протокол замера (иначе цифры не сравнимы между сессиями)

1. **Погасить фоновые потребители CPU**: `ollama` (llama-server ест ~17% ядра), `qdrant`,
   `searxng`, браузер.
2. Проверить, что машина простаивает: `uptime` (load average) и `ps -eo pcpu,comm --sort=-pcpu | head`.
3. Только после этого запускать `./bench/run_all_ab.sh` — он пишет дату, число ядер, load и версию
   `ab` в шапку `RESULTS_ab.txt`.
4. Сравнивать **соотношения** (Ferronit/FastAPI), а не абсолютные req/s из разных сессий: одна
   машина даёт ±2–3% дрейфа, а на граничных сценариях больше.

Требования: окружение `.venv` в корне репозитория, `ab` (`apt install apache2-utils`),
Docker с контейнером `ferronit-pg` на :5432.

## legacy_httpx/ — что здесь и почему не годится

`bench_network.py`, `bench_payload.py` и другие ходят httpx-клиентом, поэтому их req/s
**client-bound** (~3–7k) — пригодны для отладки ASGI-пути без сети, но не для цифр в README.

## Helper-приложения (`_`-префикс)

`_bench_rows_*.py`, `_bench_granian_*.py` — минимальные приложения,
которые запускают основные скрипты как отдельные серверы. Имена собираются динамически,
поэтому поиск по имени их «не находит». Не удаляй, не проверив `bench_*.py`.
