# Ferronit и FastAPI: один сервис, четыре реализации

Тот же контракт, что и в [сравнении с Django](vs-django.md): шесть эндпоинтов над
таблицей `bench_rows` (PostgreSQL). Здесь две реализации на FastAPI и две на
Ferronit — все в репозитории, поднимаются одной командой, отвечают одинаково.

| Вариант | Где лежит | Модель исполнения |
|---|---|---|
| **F1** — FastAPI, идиоматичный | `bench/fastapi_bench/app.py` — 1 файл | pydantic-модели, `Depends`-сессия, SQLAlchemy 2.0 async ORM |
| **F2** — FastAPI, стендовый | строка `FASTAPI_APP` в `bench/api_bench.py` | то же без pydantic: тело через `request.json()`, ответ — dict |
| **B** — Ferronit: агрегат + `Repository` | `bench/django_bench/ferronit_service.py` | Mapper/Repository/UoW, роуты вызывают репозиторий |
| **C** — Ferronit: + CQRS | `FERRONIT_APP` в `bench/api_bench.py` | то же плюс CommandBus/QueryBus и хендлеры |

Контракт (одинаковый у всех; Django — пятая точка сравнения, см. [vs-django.md](vs-django.md)):

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
| F1 — FastAPI идиоматичный | 1 | 144 | **69** |
| F2 — FastAPI стендовый | 1 | 88 | **62** |
| B — Ferronit: агрегат + `Repository` | 1 | 152 | **69** |
| C — Ferronit: + CQRS | 1 | 218 | **128** |
| A — Django (для справки) | 5 | 91 | 66 |

Обе стороны укладываются в **69 значимых строк**: разница не в объёме кода, а в
том, что каждая сторона получает даром. FastAPI отдаёт валидацию, схему и
`Depends`; Ferronit — DI из аннотаций, UoW на запрос и доменные блоки. Стендовая
версия F2 на 7 строк короче только потому, что не описывает модели запроса.

## Вариант F1 — идиоматичный FastAPI

Модели запроса/ответа описываются pydantic-ом, сессия приходит через `Depends`,
роут объявляет констрейнты прямо в подписи:

```python
class RowIn(BaseModel):
    name: str = Field(min_length=1)
    email: str = Field(min_length=1)

class RowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    email: str

async def session_dep() -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        yield session

Session = Annotated[AsyncSession, Depends(session_dep)]

@app.get("/rows")
async def rows(session: Session, limit: Annotated[int, Query(ge=1, le=1000)] = 100) -> dict[str, list[RowOut]]:
    result = await session.execute(select(BenchRow).limit(limit))
    return {"rows": [RowOut.model_validate(row) for row in result.scalars()]}

@app.post("/rows")
async def insert(body: RowIn, session: Session) -> dict[str, int]:
    row = BenchRow(**body.model_dump())
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return {"id": row.id}
```

Что FastAPI делает сам: декодирует и валидирует тело и параметры (ошибка → **422**
со списком полей), сериализует ответ по аннотации возвращаемого типа, отдаёт
`/openapi.json` и `/docs`, открывает/закрывает сессию через `Depends`, а
`asynccontextmanager`-lifespan закрывает движок на shutdown.

## Вариант F2 — стендовый FastAPI (для замеров)

`FASTAPI_APP` в `bench/api_bench.py` — тот же API, но без pydantic: тело читается
`await request.json()`, ответ собирается dict-ом вручную. Смысл: замеры в
[benchmarks.md](benchmarks.md) сняты так, чтобы ни одна ветка не получала
преимущества от «магии» фреймворка, и FastAPI в них не платил за pydantic.
Валидация при этом отсутствует — битые данные дают **500**, а не 422 (см. таблицу
ниже).

Варианты B и C — Ferronit-реализации того же сервиса; их код и разбор — в
[vs-django.md](vs-django.md), здесь они нужны только для сопоставления цифр.

## Что FastAPI даёт из коробки, а Ferronit — нет

| Возможность | FastAPI 0.143.0 | Ferronit 0.17.0 |
|---|---|---|
| Валидация входа | pydantic-модели → **422** с деталями | `msgspec.Struct` + `Annotated[...]` → **400** (мягко: поле с default можно не слать) |
| Схема API | OpenAPI 3.1 + Swagger UI из коробки (`/openapi.json`, `/docs`) | `openapi.py` из type hints (без `response_model`/`examples`) |
| Внедрение зависимостей | `Depends` (функции-зависимости, `yield` для ресурсов) | `Container`: автосборка графа по аннотациям `__init__`, singleton/factory |
| ORM | нет: SQLAlchemy 2.0 async отдельно (в F1 — `Mapped`/`mapped_column`) | нет: rawdb/rawmodel + `Repository`/`Mapper` |
| Миграции | нет: Alembic отдельно (в этом окружении не установлен) | нет: DDL руками или Alembic сбоку |
| Транзакции | сессия + `await session.commit()` | `uow()` на операцию, `readonly=True` пропускает `BEGIN` |
| Lifespan | есть (`asynccontextmanager`) | есть (`@app.lifespan`) |
| Фоновые задачи | `BackgroundTasks` | нет |
| Админка | нет | нет |
| Аутентификация | нет из коробки; есть блоки `fastapi.security` | нет; есть только `contrib/security.py` (заголовки) |
| Sync-хендлеры | `def`-роут уходит в threadpool | только async |

**Переход FastAPI → Ferronit дешевле, чем Django → Ferronit:** у FastAPI тоже нет
админки, ORM и миграций в поставке, а валидация в Ferronit встроена в подпись
хендлера так же, как pydantic-модель в FastAPI. Реальный объём работы — схема
OpenAPI (у FastAPI богаче), фон (`BackgroundTasks`) и переписывание слоя данных с
SQLAlchemy на rawdb/rawmodel.

## Поведение на некорректном вводе

Реальные коды ответов, один и тот же набор запросов:

| Запрос | F1 идиоматичный | F2 стендовый | Ferronit B |
|---|---|---|---|
| `PUT /rows/abc` (не `int`) | 422 | 422 | 404 |
| `POST /rows` с битым JSON | 422 | **500** | 400 |
| `POST /rows` без поля `name` | 422 | **500** | 200 (`name=""` — default) |
| `POST /rows` с `{"name": 1}` | 422 | **500** | 400 |
| `GET /rows?limit=abc` | 422 | 422 | 400 |
| `GET /rows?limit=-1` | 422 (`ge=1`) | **500** | 500 (`LIMIT must not be negative`) |
| `GET /rows?limit=0` | 422 (констрейнт) | 200 (пустой список) | 200 (пустой список) |

Наблюдения: FastAPI валидирует **и path, и query, и тело** одним механизмом —
поэтому `/rows/abc` даёт 422, а не 404, и констрейнт `Query(ge=1)` закрывает
`limit=-1` (у Ferronit для этого нужен `msgspec.Meta(ge=1)` в query-модели, иначе
отрицательный лимит уходит в SQL). Зато ответ FastAPI — машиночитаемый список
ошибок pydantic, а Ferronit отдаёт короткий 400 без деталей.

## Асинхронность

- FastAPI async-first, но `def`-роуты не блокируют цикл: `fastapi/routing.py`
  оборачивает их в `run_in_threadpool` → `anyio.to_thread.run_sync` (проверено по
  исходнику установленной версии). То есть цена sync-хендлера — поток, как и в Django.
- SQLAlchemy 2.0 async — настоящий async поверх asyncpg; ORM-слой остаётся главным
  потребителем времени на строках (см. таблицу ниже: `limit=1000` — 213 req/s у F1
  против 1 237 у Ferronit с явным SQL).
- Ferronit async-only: sync-хендлера нет вовсе, поэтому «случайно заблокировать цикл»
  нечем.

## Производительность

Свежий прогон: granian, 1 воркер, `ab -c 50 -k`, лучшее из 3, таблица
`bench_rows` — 1000 строк, одна машина, load ~1.6:

| Вариант | `GET /ping` | `GET /rows?limit=100` | `GET /rows?limit=1000` |
|---|---|---|---|
| F1 — FastAPI идиоматичный | 22 687 | 1 207 | 213 |
| F2 — FastAPI стендовый | 18 619 | 855 | 146 |
| B — Ferronit (Repository) | **92 058** | **6 456** | **1 237** |
| Отношение B / F1 | ×4.1 | ×5.3 | ×5.8 |

Абсолюты шумят между сессиями (±2–3%) — сравнивать стоит отношения внутри одного
прогона, как здесь. Публикация цифр по всему стенду (включая записи и остальные
фреймворки) — в [benchmarks.md](benchmarks.md); там FastAPI-ветка рядом с Django и
BlackSheep. По записям из того прогона картина неровная: INSERT/DELETE у Ferronit
кратно быстрее (×4.7 и ×4.3), UPSERT — ×1.4, а UPDATE у FastAPI даже выше
(1 519 против 946) за счёт сессионного кэша, то есть выигрыш Ferronit — не «на всём»,
а на горячем пути и чтениях.

## Когда что выбирать

- **FastAPI** — нужна валидация и схема «из коробки», богатая экосистема и найм;
  профиль сервиса — не только JSON+БД (файлы, фон, интеграции), а нагрузка
  укладывается в тысячи req/s.
- **Ferronit** — узкий горячий API (JSON + SQL) под нагрузкой, где важны единицы
  тысяч req/s на воркер, а взамен готовы писать слой данных и миграции сами;
  доменная логика уже требует агрегатов/шин.

## Как воспроизвести

```bash
# 1. PostgreSQL + таблица 1000 строк (как в стенде)
docker start ferronit-pg || docker run -d --name ferronit-pg \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_USER=postgres -e POSTGRES_DB=postgres \
  -p 5432:5432 postgres:18
.venv/bin/python bench/api_bench.py            # seed(): CREATE TABLE bench_rows + 1000 строк

# 2. F1 — FastAPI идиоматичный
.venv/bin/python -m uvicorn app:app --port 8322 --app-dir bench/fastapi_bench
curl -s localhost:8322/ping
curl -s "localhost:8322/rows?limit=3"
curl -s -X POST localhost:8322/rows -H 'content-type: application/json' -d '{"name":"ada","email":"a@x"}'
curl -s localhost:8322/openapi.json | head -c 200

# 3. F2 — FastAPI стендовый (файл пишет api_bench.py)
.venv/bin/python -m granian --interface asgi --no-ws --working-dir bench _bench_api_fastapi:app --port 8323

# 4. B — Ferronit (агрегат + Repository)
.venv/bin/python -m uvicorn django_bench.ferronit_service:app --port 8321 --app-dir bench

# 5. Замеры в одном окне
ab -n 20000 -c 50 -k localhost:8322/ping
ab -n 10000 -c 50 -k "localhost:8321/rows?limit=1000"

# 6. Объём кода всех вариантов
.venv/bin/python bench/loc_compare.py
```

Смежные доки: [Ferronit и Django](vs-django.md), [Ferronit и BlackSheep](vs-blacksheep.md),
[бенчмарки](benchmarks.md), [Litestar](vs-litestar.md), [Tornado](vs-tornado.md),
[Twisted](vs-twisted.md), [слой данных](data/rawdb.md),
[DDD и DI](ddd.md).
