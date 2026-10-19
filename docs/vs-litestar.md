# Ferronit и Litestar: один сервис, четыре реализации

Тот же контракт, что и в сравнениях [с Django](vs-django.md),
[FastAPI](vs-fastapi.md) и [BlackSheep](vs-blacksheep.md): шесть эндпоинтов над
таблицей `bench_rows` (PostgreSQL). Здесь две реализации на Litestar и две на
Ferronit.

| Вариант | Где лежит | Модель исполнения |
|---|---|---|
| **LS1** — Litestar, идиоматичный | `bench/litestar_bench/app.py` — 1 файл | msgspec-модели тела, DI через `Provide`, `Parameter` с констрейнтами |
| **LS2** — Litestar, стендовый | строка `LITESTAR_APP` в `bench/api_bench.py` | сырой asyncpg + msgspec, тело через `request.json()`, без DI |
| **B** — Ferronit: агрегат + `Repository` | `bench/django_bench/ferronit_service.py` | Mapper/Repository/UoW, роуты вызывают репозиторий |
| **C** — Ferronit: + CQRS | `FERRONIT_APP` в `bench/api_bench.py` | то же плюс CommandBus/QueryBus и хендлеры |

| Метод и путь | Ответ | Litestar по умолчанию |
|---|---|---|
| `GET /ping` | `{"ok": true}` | 200 |
| `GET /rows?limit=N` | `{"rows": [...]}` | 200 |
| `POST /rows` | `{"id": N}` | **201** (не 200, как у остальных) |
| `PUT /rows/{id}` | `{"id": N}` | 200 |
| `DELETE /rows/{id}` | `{"ok": true}` | 200 (в LS1/LS2 явно `status_code=200`) |
| `POST /rows/upsert` | `{"id": N}` | 201 |

## Цена в строках

Подсчёт — AST-обход (`bench/loc_compare.py`): без пустых строк, комментариев и
докстрингов.

| Вариант | Файлов | Всего строк | Значимых |
|---|---|---|---|
| LS1 — Litestar идиоматичный | 1 | 168 | **91** |
| LS2 — Litestar стендовый | 1 | 81 | **56** |
| B — Ferronit: агрегат + `Repository` | 1 | 152 | **69** |
| C — Ferronit: + CQRS | 1 | 218 | **128** |

LS1 — самый длинный «сервисный» вариант из четырёх сравнений (91 значимая строка):
DI и типизация в Litestar не убирают ручной SQL, зато требуют явного провайдера и
класса репозитория. LS2 (56 строк) — самый короткий: ни DI, ни bindings, ни моделей.

## Вариант LS1 — идиоматичный Litestar

Тело — обычный `msgspec.Struct`: Litestar принимает его напрямую, без pydantic и без
DTO (проверено прогоном), поэтому модели те же, что и в Ferronit.

```python
class RowIn(msgspec.Struct):
    name: str
    email: str

class RowsRepo:                       # ленивый пул asyncpg внутри
    ...

async def provide_repo() -> RowsRepo:
    """DI-провайдер: Litestar передаёт результат в параметр `repo`."""
    return _repo

@get("/rows")
async def rows(repo: RowsRepo, limit: int = Parameter(default=100, ge=1, le=1000)) -> dict:
    return {"rows": await repo.list(limit)}

@post("/rows")
async def insert(data: RowIn, repo: RowsRepo) -> dict:
    return {"id": await repo.insert(data.name, data.email)}

app = Litestar(
    route_handlers=[ping, rows, insert, update, remove, upsert],
    dependencies={"repo": Provide(provide_repo)},
    lifespan=[lifespan],              # закрывает пул на shutdown
)
```

Что Litestar делает сам: валидирует тело и параметры по аннотациям (ошибка →
**400** с `extra`-описанием), резолвит `repo` из DI, сериализует ответ, отдаёт
`/schema/openapi.json` и `/schema/swagger`. Путь пишется как `{row_id:int}` —
несовпадающий с конвертером путь даёт **404** (у Ferronit так же).

## Вариант LS2 — стендовый Litestar

`LITESTAR_APP` в `bench/api_bench.py`: пул с ленивым `get_pool()`, `await
request.json()` вместо моделей, `dict(r)` вместо проекций, `asyncpg` без DI. Так
сняты цифры в [benchmarks.md](benchmarks.md) — ни одна ветка не получает
преимущества от «магии» фреймворка. Схема OpenAPI при этом всё равно генерируется:
`/schema/openapi.json` в этом варианте отвечает 200.

## Что Litestar даёт из коробки

| Возможность | Litestar 2.24 | Ferronit 0.17.0 |
|---|---|---|
| Валидация входа | по аннотациям (msgspec/pydantic/dataclass), ошибка → **400** с `extra` | `msgspec.Struct` + `Annotated`, ошибка → **400**, path → **404** |
| Схема API | OpenAPI 3.1 + Swagger/ReDoc/Scalar (`/schema/*`) | `openapi.py` из type hints |
| DI | `Provide`/`dependencies`, `lifespan`-зависимости | `Container`: автосборка по аннотациям `__init__` |
| DTO-слой | `DataclassDTO`, `MsgspecDTO`, `DTOConfig` (входят в пакет; в LS1 не понадобились — `msgspec.Struct` принимается напрямую) | нет: модели — это сами структуры |
| Аутентификация | `SessionAuth` (есть); `JWTAuth` — нужен extra `pyjwt`, cookie-сессии — extra `cryptography` | нет |
| CSRF / rate limit | `CSRFConfig`, `RateLimitConfig` | нет |
| CORS / сжатие / статика | `CORSConfig`, `CompressionConfig`, `StaticFilesConfig` | `contrib/cors.py`, gzip в Rust-ядре, `contrib/staticfiles.py` |
| Каналы (pub/sub), WebSocket | `ChannelsPlugin`, WS-роуты | WebSocket-хендлеры, без pub/sub |
| ORM | `SQLAlchemyPlugin` — требует пакет `advanced_alchemy` (не установлен) | нет ORM: rawdb/rawmodel |
| Админка, миграции, фон | нет | нет |
| Тест-клиент | `litestar.testing.TestClient` | ASGI-диалог (`tests/test_examples.py`) |

Litestar — самый «батареечный» из четырёх фреймворков сравнения, но часть батареек
за extras: JWT (`pyjwt`), cookie-сессии (`cryptography`), ORM-плагин
(`advanced_alchemy`). В админке, миграциях и фоновых задачах — паритет с Ferronit.

## Поведение на некорректном вводе

Реальные коды ответов (Ferronit — вариант B):

| Запрос | LS1 идиоматичный | LS2 стендовый | Ferronit B |
|---|---|---|---|
| `PUT /rows/abc` (не `int`) | 404 | 404 | 404 |
| `POST /rows` с битым JSON | 400 (`JSON is malformed`) | **500** | 400 |
| `POST /rows` без поля `name` | 400 (`Object missing required field`) | **500** | 200 (`name=""` — default) |
| `POST /rows` с `{"name": 1}` | 400 (`Expected str, got int`) | **500** | 400 |
| `GET /rows?limit=abc` | 400 | 400 | 400 |
| `GET /rows?limit=-1` | 400 (`ge=1`) | **500** | 500 (`LIMIT must not be negative`) |
| `GET /rows?limit=0` | 400 (констрейнт) | 200 (пустой список) | 200 (пустой список) |

Отличия от соседних сравнений: у Litestar ошибки **400** с машиночитаемым
`extra`-списком (у FastAPI — 422, у BlackSheep — короткий текст, у Ferronit — 400
без деталей), и `limit` можно ограничить констрейнтом в подписи хендлера
(`Parameter(ge=1)`), чего у Ferronit требует query-модель с `msgspec.Meta`.

## Асинхронность

- Litestar async-first, но sync-хендлеры не блокируют цикл: они оборачиваются в
  `sync_to_thread` (anyio-threadpool) — поведение как у Django и FastAPI; отключается
  опцией `sync_to_thread=False`.
- Litestar принимает `msgspec.Struct` как модель тела без DTO-обёртки — это его
  штатная возможность, поэтому валидация и сериализация идут C-кодеком msgspec,
  как в Ferronit.
- Ferronit async-only: sync-хендлера нет вовсе.

## Производительность

Свежий прогон: granian, 1 воркер, `ab -c 50 -k`, лучшее из 3, таблица
`bench_rows` — 1000 строк, одна машина, load 0.24:

| Вариант | `GET /ping` | `GET /rows?limit=100` | `GET /rows?limit=1000` |
|---|---|---|---|
| LS1 — Litestar идиоматичный | 39 001 | 6 206 | 1 665 |
| LS2 — Litestar стендовый | 39 269 | 6 654 | 1 628 |
| B — Ferronit (Repository) | **94 277** | 6 892 | 1 363 |
| Отношение | B ×2.4 | B ×1.04…×1.11 | LS1 ×1.22 / LS2 ×1.19 |

Как это читать:

- **На пустом маршруте — разрыв ×2.4.** Litestar стабильно держит ~39k req/s против
  94k у Ferronit; публикация стенда даёт то же соотношение (×2.6 на `/ping`).
- **На чтениях из БД — паритет и нюансы слоя.** На `limit=100` все три варианта в
  пределах 1.04–1.11. На `limit=1000` Litestar-варианты впереди Ferronit B:
  1 628–1 665 против 1 363. Причина не в фреймворке, а в проекции: LS1/LS2 отдают
  `dict(r)` сразу из asyncpg, а Ferronit B собирает агрегаты через
  `Repository`/`Mapper`. Публикация [benchmarks.md](benchmarks.md) сравнивает
  «сырые» ветки (прогон 16.10.2026): Ferronit-стенд (rawmodel) впереди на `limit=1`
  — 12 540 против 10 828, но отстаёт на `limit=100` (5 674 против 6 340) и
  `limit=1000` (1 179 против 1 496): у Litestar-стенда тот же сырой asyncpg-путь.
- Вывод тот же, что в сравнении с BlackSheep: на чтениях решает слой данных, а не
  имя фреймворка; на горячем пути (JSON, роутинг) Ferronit вне конкуренции с
  Python-конкурентами в этом стенде.

## Когда что выбирать

- **Litestar** — нужны DI, DTO-слой, OpenAPI, CSRF/rate limit/сессии/каналы и
  единый стиль «всё в подписи хендлера»; 39k req/s на пустом маршруте достаточно,
  а на чтениях из БД разрыв с Ferronit невелик.
- **Ferronit** — тот же msgspec-кодек и типизация, но ~94k req/s на горячем пути,
  Rust-ядро (JSON/gzip/CORS) и доменные блоки (DDD/CQRS, UoW); данных слой —
  rawdb/rawmodel без ORM.
- **Ни то, ни другое** — если нужны ORM-экосистема, миграции и админка: это
  [Django](vs-django.md) либо SQLAlchemy + Alembic рядом с любым из фреймворков.

## Как воспроизвести

```bash
# 1. PostgreSQL + таблица 1000 строк (как в стенде)
docker start ferronit-pg || docker run -d --name ferronit-pg \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_USER=postgres -e POSTGRES_DB=postgres \
  -p 5432:5432 postgres:18
.venv/bin/python bench/api_bench.py            # seed(): CREATE TABLE bench_rows + 1000 строк

# 2. LS1 — Litestar идиоматичный
.venv/bin/python -m uvicorn app:app --port 8340 --app-dir bench/litestar_bench
curl -s localhost:8340/ping
curl -s "localhost:8340/rows?limit=3"
curl -s -X POST localhost:8340/rows -H 'content-type: application/json' -d '{"name":"ada","email":"a@x"}'
curl -s -o /dev/null -w '%{http_code}\n' localhost:8340/schema/swagger   # 200 (UI)

# 3. LS2 — Litestar стендовый (файл пишет api_bench.py)
.venv/bin/python -m granian --interface asgi --no-ws --working-dir bench _bench_api_litestar:app --port 8341

# 4. B — Ferronit (агрегат + Repository)
.venv/bin/python -m uvicorn django_bench.ferronit_service:app --port 8321 --app-dir bench

# 5. Замеры в одном окне (только ab: Python-клиент меряет себя)
ab -n 20000 -c 50 -k localhost:8340/ping
ab -n 20000 -c 50 -k "localhost:8341/rows?limit=100"
ab -n 10000 -c 50 -k "localhost:8321/rows?limit=1000"

# 6. Объём кода всех вариантов
.venv/bin/python bench/loc_compare.py
```

Смежные доки: [Ferronit и Django](vs-django.md), [Ferronit и FastAPI](vs-fastapi.md),
[Ferronit и BlackSheep](vs-blacksheep.md), [Tornado](vs-tornado.md), [Twisted](vs-twisted.md),
[бенчмарки](benchmarks.md).
