# Ferronit и BlackSheep: один сервис, четыре реализации

Тот же контракт, что и в [сравнении с Django](vs-django.md) и
[с FastAPI](vs-fastapi.md): шесть эндпоинтов над таблицей `bench_rows`
(PostgreSQL). BlackSheep — основной конкурент Ferronit по роутингу (разрыв
×1.03–1.1 на пустых маршрутах), поэтому здесь сравниваются две реализации на
BlackSheep и две на Ferronit.

| Вариант | Где лежит | Модель исполнения |
|---|---|---|
| **BS1** — BlackSheep, идиоматичный | `bench/blacksheep_bench/app.py` — 1 файл | DI-сервис в `app.services`, `FromQuery`, dataclass-модели, OpenAPI |
| **BS2** — BlackSheep, стендовый | строка `BLACKSHEEP_APP` в `bench/api_bench.py` | сырой asyncpg + msgspec, без bindings и DI |
| **B** — Ferronit: агрегат + `Repository` | `bench/django_bench/ferronit_service.py` | Mapper/Repository/UoW, роуты вызывают репозиторий |
| **C** — Ferronit: + CQRS | `FERRONIT_APP` в `bench/api_bench.py` | то же плюс CommandBus/QueryBus и хендлеры |

Контракт (одинаковый у всех):

| Метод и путь | Ответ |
|---|---|
| `GET /ping` | `{"ok": true}` |
| `GET /rows?limit=N` | `{"rows": [...]}` |
| `POST /rows` | `{"id": N}` |
| `PUT /rows/{id}` | `{"id": N}` |
| `DELETE /rows/{id}` | `{"ok": true}` |
| `POST /rows/upsert` | `{"id": N}` |

## Цена в строках

Подсчёт — AST-обход (`bench/loc_compare.py`): без пустых строк, комментариев и
докстрингов.

| Вариант | Файлов | Всего строк | Значимых |
|---|---|---|---|
| BS1 — BlackSheep идиоматичный | 1 | 169 | **86** |
| BS2 — BlackSheep стендовый | 1 | 87 | **60** |
| B — Ferronit: агрегат + `Repository` | 1 | 152 | **69** |
| C — Ferronit: + CQRS | 1 | 218 | **128** |

BS1 — самый длинный из «сервисных» вариантов: DI и bindings не убирают ручной
SQL, а классу репозитория нужны пять методов. Разница BS1 → BS2 (86 → 60 строк) —
это ровно DI, типизация параметров и датаклассы тела.

## Вариант BS1 — идиоматичный BlackSheep

```python
app = Application()
app.services.add_instance(RowsRepo(DSN))          # сервис виден DI-контейнеру
docs = OpenAPIHandler(info=Info(title="bench", version="0.1.0"), ui_path="/docs")
docs.bind_app(app)


@app.router.get("/rows")
async def rows(repo: RowsRepo, limit: int = FromQuery(100)) -> dict:
    return {"rows": await repo.list(limit)}


@app.router.post("/rows")
async def insert(body: RowIn, repo: RowsRepo) -> dict[str, int]:
    return {"id": await repo.insert(body.name, body.email)}
```

Что BlackSheep делает сам: резолвит `RowsRepo` из DI-контейнера по аннотации
типа, разбирает и типизирует query/path/тело (несоответствие → **400**),
сериализует ответ (dict, список, dataclass), отдаёт `/openapi.json` и `/docs`
(сам UI по `/docs/`).

**Замечание по версиям (проверено прогоном):** на связке blacksheep 2.6.4 +
CPython 3.14.4 аннотации `From*[T]` не резолвятся — параметр приходит в хендлер
объектом bindings (`limit: FromQuery[int]`, `body: FromJSON[RowIn]`,
`repo: FromServices[Repo]` дают 500). Работают: тип сервиса как аннотация
(`repo: RowsRepo`), простой тип для пути (`row_id: int`) и `FromQuery(...)` как
значение по умолчанию. В файле использованы только рабочие формы.

## Вариант BS2 — стендовый BlackSheep

`BLACKSHEEP_APP` в `bench/api_bench.py`: пул asyncpg с ленивой инициализацией,
`request.json()` вместо bindings, `msgspec.json.encode` вместо нормализации
ответа. Так сняты цифры в [benchmarks.md](benchmarks.md) — чтобы ни одна ветка не
получала преимущества от «магии» фреймворка. DI и типизации здесь нет вообще:
невалидные данные падают 500-й (кроме тела — JSON разбирает сам BlackSheep).

## Что BlackSheep даёт из коробки

| Возможность | BlackSheep 2.6.4 | Ferronit 0.17.0 |
|---|---|---|
| DI | `app.services`: `register`/`add_instance`/`add_singleton*`/`add_scoped*`; параметр резолвится по типу или имени | `Container`: автосборка графа по аннотациям `__init__`, singleton/factory, `Annotated`-ключи |
| Типизация входа | bindings (`FromQuery`/`FromRoute`/`FromJSON`/…), ошибка → **400** | `msgspec.Struct` + `Annotated`, ошибка → **400**, path → **404** |
| Схема API | OpenAPI 3.1 + Swagger UI (`/openapi.json`, `/docs`) | `openapi.py` из type hints |
| Аутентификация | `use_authentication(SecurityPolicyHandler)` + `Identity` | нет |
| Сессии | `use_sessions(...)` | нет |
| CORS / gzip / статика | `app.use_cors`, `use_gzip_compression(app)`, `app.serve_files` | `contrib/cors.py`, gzip в Rust-ядре, `contrib/staticfiles.py` |
| Контроллеры | `use_controllers()` | нет (только функции-хендлеры) |
| ORM, миграции, админка, фон | нет | нет |
| Клиент для тестов | `blacksheep.client.ClientSession` (in-process) | ASGI-диалог (`tests/test_examples.py`) |

По «батарейкам» BlackSheep и Ferronit стоят рядом: ни там, ни там нет ORM,
миграций, админки и фоновых задач. Разница — в аутентификации/сессиях у
BlackSheep и в доменных блоках (DDD/DI/UoW) у Ferronit.

## Поведение на некорректном вводе

Реальные коды ответов (Ferronit — вариант B):

| Запрос | BS1 идиоматичный | BS2 стендовый | Ferronit B |
|---|---|---|---|
| `PUT /rows/abc` (не `int`) | 400 | **500** | 404 |
| `POST /rows` с битым JSON | 400 | 400 | 400 |
| `POST /rows` без поля `name` | 400 | **500** | 200 (`name=""` — default) |
| `POST /rows` с `{"name": 1}` | 400 | **500** | 400 |
| `GET /rows?limit=abc` | 400 | **500** | 400 |
| `GET /rows?limit=-1` | 500 (уходит в SQL) | 500 | 500 |
| `GET /rows?limit=0` | 200 (пустой список) | 200 (пустой список) | 200 (пустой список) |

BlackSheep возвращает именно **400** (не 422, как FastAPI) и без структуры ошибок
pydantic — короткое текстовое сообщение. Типизацию даёт только bindings-вариант;
в стендовом всё, что не JSON-разбор, доходит до кода и падает.

## Асинхронность

BlackSheep async-first, но **sync-хендлер выполняется прямо в цикле событий**:
`get_sync_wrapper` в `blacksheep/server/normalization.py` оборачивает `def`-роут в
`async def handler(...): return method(...)` без ухода в поток. То есть блокирующий
код внутри sync-хендлера останавливает и остальные запросы — в отличие от Django и
FastAPI, где sync уходит в threadpool. Ferronit async-only: sync-хендлера нет,
случайно заблокировать цикл нечем.

## Производительность

Свежий прогон: granian, 1 воркер, `ab -c 50 -k`, лучшее из 3, таблица
`bench_rows` — 1000 строк, одна машина, load 0.29:

| Вариант | `GET /ping` | `GET /rows?limit=100` | `GET /rows?limit=1000` |
|---|---|---|---|
| BS1 — BlackSheep идиоматичный | 92 740 | 6 076 | 1 185 |
| BS2 — BlackSheep стендовый | 91 898 | 8 952 | 1 758 |
| B — Ferronit (Repository) | **95 681** | 7 021 | 1 395 |
| Отношение | B ×1.03 / ×1.04 | B к BS1 ×1.16, BS2 к B ×1.28 | B к BS1 ×1.18, BS2 к B ×1.26 |

Как это читать:

- **На пустом маршруте — паритет.** Разница с BlackSheep ×1.03–1.04, то есть в
  пределах шума; это согласуется с публикацией стенда (×1.05–1.1).
- **На строках выигрывает «сырой» вариант, а не фреймворк.** BS2 (asyncpg +
  msgspec, без bindings/DI) быстрее сервисного Ferronit B в 1.26–1.28×, а Ferronit B
  быстрее идиоматичного BS1 в 1.16–1.18×. Причина — не язык и не роутер, а слой:
  `Repository`/`Mapper` собирает агрегаты, bindings-вариант BlackSheep тратит время
  на нормализацию. Публикация [benchmarks.md](benchmarks.md) (тот же стенд, прогон
  16.10.2026) даёт на `limit=1000` для Ferronit-стенда (rawmodel) **1 179 req/s
  против 1 588** у BlackSheep — сравниваются именно «сырые» ветки, и впереди
  BlackSheep: сырой asyncpg остаётся быстрее слоя моделей. Таблица выше — свой
  прогон (BS1/BS2/B на одном сервисе), числа из разных прогонов не смешивать.
- Практический вывод: если нужен максимум req/s на чтениях, выбирайте не
  фреймворк, а слой доступа к данным (rawdb/rawmodel или сырой asyncpg) — оба
  варианта на одном сервере дают 90–96k на ping.

## Когда что выбирать

- **BlackSheep** — компактный async-микросервис с DI, типизированными параметрами,
  OpenAPI, CORS/сессиями/аутентификацией из коробки, на ~90k req/s; зрелая
  библиотека с историей.
- **Ferronit** — те же ~92–96k на ping плюс доменные блоки (DDD/CQRS, порты и
  адаптеры, UoW) и Rust-ядро под JSON/gzip/CORS; данных слой — rawdb/rawmodel
  без ORM.
- **Ни то, ни другое** — если сервису нужны админка, ORM и миграции: это
  [Django](vs-django.md) (или SQLAlchemy + Alembic рядом с любым из этих
  фреймворков).

## Как воспроизвести

```bash
# 1. PostgreSQL + таблица 1000 строк (как в стенде)
docker start ferronit-pg || docker run -d --name ferronit-pg \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_USER=postgres -e POSTGRES_DB=postgres \
  -p 5432:5432 postgres:18
.venv/bin/python bench/api_bench.py            # seed(): CREATE TABLE bench_rows + 1000 строк

# 2. BS1 — BlackSheep идиоматичный
.venv/bin/python -m uvicorn app:app --port 8324 --app-dir bench/blacksheep_bench
curl -s localhost:8324/ping
curl -s "localhost:8324/rows?limit=3"
curl -s -X POST localhost:8324/rows -H 'content-type: application/json' -d '{"name":"ada","email":"a@x"}'
curl -s -o /dev/null -w '%{http_code}\n' -L localhost:8324/docs      # 200 (UI)

# 3. BS2 — BlackSheep стендовый (файл пишет api_bench.py)
.venv/bin/python -m granian --interface asgi --no-ws --working-dir bench _bench_api_blacksheep:app --port 8325

# 4. B — Ferronit (агрегат + Repository)
.venv/bin/python -m uvicorn django_bench.ferronit_service:app --port 8321 --app-dir bench

# 5. Замеры в одном окне (только ab: Python-клиент меряет себя)
ab -n 20000 -c 50 -k localhost:8324/ping
ab -n 20000 -c 50 -k "localhost:8325/rows?limit=100"
ab -n 10000 -c 50 -k "localhost:8321/rows?limit=1000"

# 6. Объём кода всех вариантов
.venv/bin/python bench/loc_compare.py
```

Смежные доки: [Ferronit и Django](vs-django.md), [Ferronit и FastAPI](vs-fastapi.md),
[Ferronit и Litestar](vs-litestar.md), [Tornado](vs-tornado.md), [Twisted](vs-twisted.md),
[бенчмарки](benchmarks.md), [слой данных](data/rawdb.md).
