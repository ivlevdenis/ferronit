# Стенд замеров Ferronit

Здесь лежат скрипты, которыми получены цифры в README. Это **не часть пакета**: черновой
измерительный код, который не линтуется (`ruff` исключает `bench/`) и не попадает в
sdist/Docker-образ. Логика фреймворка от него не зависит.

**Основной конкурент для сравнения — BlackSheep.** Бенчи по умолчанию сравнивают Ferronit
с BlackSheep (ближайший соперник), остальные (Litestar/FastAPI/Django) — дополнительные точки.

## Политика: req/s меряем только `ab`

**Единственный источник цифр по HTTP — ApacheBench (`ab`, C-клиент).** Python-клиент
(httpx/asyncio) сам упирается в ~3.5–3.7k req/s независимо от сервера: в таких замерах
измеряется клиент, а не фреймворк. Поэтому httpx-скрипты переехали в `legacy_httpx/`
и **не используются для сравнения**.

## Основное

```bash
./bench/run_all_ab.sh                     # ping + rows?limit + записи, ~5–6 минут, пишет bench/RESULTS_ab.txt
.venv/bin/python bench/api_bench.py                     # то же без записи в файл
BENCH_APPS=ferronit,blacksheep .venv/bin/python bench/api_bench.py   # выборочно
```

`api_bench.py` генерирует одинаковые приложения (Ferronit, BlackSheep, Litestar, FastAPI, Django),
поднимает их granian'ом (1 воркер; Django — через `django_bench.asgi`), греет и меряет по очереди:

- `GET /ping` — простой `{"ok": true}`;
- `GET /rows?limit=N` — `SELECT id, name, email LIMIT $1`, где `N` (1/20/50/100/1000) приходит
  **query-параметром**, как в реальном API;
- одиночные записи: `POST /rows` (INSERT), `PUT /rows/{id}` (UPDATE), `DELETE /rows/{id}`,
  `POST /rows/upsert` (UPSERT).

Перед каждой операцией записи таблица восстанавливается (`TRUNCATE` + 1000 строк), чтобы
UPDATE/UPSERT/DELETE били по существующим строкам, а не по пустой таблице.

| Скрипт | Что мерит |
|---|---|
| `api_bench.py` | ping + строки из БД (query-limit) + одиночные записи — Ferronit vs BlackSheep/Litestar/FastAPI/Django, **на `ab`** |
| `api_bench_logging.py` | ping с access-логированием (одна строка на запрос в stdout) — Ferronit (свой неблокирующий `Logger`) vs stdlib-логирование остальных фреймворков, **на `ab`** |
| `queries_vs_sqlalchemy.py` | запросы к БД **без HTTP**: Ferronit (rawmodel/rawdb) vs SQLAlchemy Core/ORM vs сырой asyncpg; `get_1`/`list_100`/`insert_1`/`update_1`/`upsert_1`/`delete_1`, 1 клиент |
| `http_cpu_bench.py` | CPU **под HTTP-нагрузкой** (`ab -c 50`): сервер (процесс приложения), PostgreSQL (cgroup), клиент (rusage) — мкс на запрос |
| `micro_bench_sql_compile.py` | цена сборки SQL в слое Ferronit (0,9 мкс на оператор) |
| `_probe_pg_cpu.py` | CPU PostgreSQL на запрос (cgroup контейнера) рядом с CPU клиента |
| `_probe_write_tuning.py`, `_probe_write_tuning2.py` | чем разгоняется запись: `synchronous_commit=off`, `fsync=off`, UNLOGGED, батчинг, групповой коммит (50 клиентов) |
| `_probe_batch_durability.py`, `_probe_batch_limits.py`, `_probe_copy.py` | батчинг и сохранность: атомарность, SIGKILL-опыт с крахом контейнера, предел параметров, `COPY` против multi-row `INSERT` |
| `_probe_copy_semantics.py`, `_probe_copy_transaction.py` | семантика `COPY … FROM STDIN`: участие в транзакции, атомарность пачки, `serial`/триггеры, отсутствие `RETURNING` (**COPY не берём — решение 16.10.2026, пробники оставлены как справка**) |
| `pg_wire.py`, `wire_bench.py` | самодельный клиент по проводу PostgreSQL (SCRAM, simple/extended, пайплайн) против asyncpg |
| `rust_pg_orm/`, `rust_pg_1row/`, `go_pg_orm/`, `go_pg_1row/` | кросс-языковые стенды: `tokio-postgres`/`sqlx`/`SeaORM`, `pgx`/`sqlx`/`GORM`/`ent` |
| `prof_*.py` | профилирование горячего пути (cProfile) |
| `legacy/` | старые HTTP-бенчи (маршрутизация, payload, bulk-строки, потолок granian) — не актуализируются |
| `legacy_httpx/` | httpx-клиент (client-bound, ~3.7k потолок) |

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

## HTTP-сравнение фреймворков (`api_bench.py`)

Канонические цифры (ping, строки с query-limit, одиночные записи) — в
[`docs/benchmarks.md`](../docs/benchmarks.md), датированы прогоном и не дублируются здесь,
чтобы не было двух расходящихся источников. Воспроизведение:

```bash
.venv/bin/python bench/api_bench.py
```

## Протокол замера (иначе цифры не сравнимы между сессиями)

1. **Погасить фоновые потребители CPU**: `ollama` (llama-server ест ~17% ядра), `qdrant`,
   `searxng`, браузер.
2. Проверить, что машина простаивает: `uptime` (load average) и `ps -eo pcpu,comm --sort=-pcpu | head`.
3. Только после этого запускать `./bench/run_all_ab.sh` — он пишет дату, число ядер, load и версию
   `ab` в шапку `RESULTS_ab.txt`.
4. Сравнивать **соотношения** (Ferronit/FastAPI), а не абсолютные req/s из разных сессий: одна
   машина даёт ±2–3% дрейфа, а на граничных сценариях больше.
5. **Замеры записи** (`queries_vs_sqlalchemy.py`, `api_bench.py --writes`) требуют снятого
   фонового I/O: autovacuum на тестовой таблице выключен, перед каждой операцией `CHECKPOINT`
   и пауза ~5 с. Иначе записи врут втрое (930 → 330 запросов/с) — подробности в
   [`docs/benchmarks.md`](../docs/benchmarks.md).

Требования: окружение `.venv` в корне репозитория, `ab` (`apt install apache2-utils`),
Docker с контейнером `ferronit-pg` на :5432.

## legacy/ и legacy_httpx/ — старые бенчи

`legacy/` — прежние HTTP-бенчи (`ab_bench.py` маршрутизация/payload, `bench_rows.py` bulk-строки,
`bench_granian_bare.py` потолок, `bench_params.py`), заменены `api_bench.py` и не актуализируются.
`legacy_httpx/` — скрипты на httpx-клиенте, req/s **client-bound** (~3–7k), для цифр не годятся.

## Helper-приложения (`_`-префикс)

`_bench_api_*.py` (и `_bench_rows_*.py` из legacy) — минимальные приложения, которые
запускают основные скрипты как отдельные серверы. Имена собираются динамически, поэтому
поиск по имени их «не находит». Не удаляй, не проверив `bench_*.py`.
