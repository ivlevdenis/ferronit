# Архитектура Ferronit

Единый обзор архитектуры: как устроен фреймворк целиком, куда идёт запрос и где
лежат строительные блоки. Детали каждого слоя вынесены в отдельные доки — ссылки
в конце. Этот файл — карта, а не дубль тех доков.

## Обзор

Ferronit — высокопроизводительный ASGI-фреймворк с поддержкой DDD. Горячий путь
живёт в Rust, доменная логика — в Python.

| Слой | Технология | Зачем |
|---|---|---|
| Сервер | granian (uvicorn — fallback) | продакшен-ASGI, 1 воркер ~100k req/s |
| Rust-ядро | `ferronit._core` (maturin, `ferronit-rs/`) | radix-роутинг, gzip, CORS, multipart, разбор query/headers |
| Ядро Python | `ferronit/core/` | ASGI-диспетчер, Request/Response |
| Сериализация | msgspec (C fast-path), fallback stdlib `json` | единый C fast-path в Python |
| БД | rawdb/rawmodel + драйвер (asyncpg / aiomysql / aiosqlite — extras) | явный SQL, типизированные модели, без ORM |
| Домен | `ddd.py`, `hexagonal.py` | Command/Query, агрегаты, порты/адаптеры, UoW |
| Конфиг/CLI | `config.py`, `cli.py` | EnvConfig, генератор проекта, dev/run |

**Принципы:**

1. **Горячий путь — на Rust.** Маршрутизация, разбор запроса, gzip, CORS и
   multipart не проходят через Python на каждый запрос.
2. **Python — только бизнес-логика.** Хендлер получает уже распарсенный запрос и
   возвращает данные; dict/list и msgspec-модели сериализует msgspec (C fast-path),
   а stdlib `json` (default=str) — fallback для dataclass/rawmodel и неподдерживаемых типов.
3. **Явный SQL вместо ORM.** rawdb/rawmodel поверх драйвера (PostgreSQL, MySQL
   или SQLite) — максимум throughput и предсказуемость.
4. **Домен отделён от HTTP.** DDD/гексагональные блоки поставляются из коробки,
   но не навязываются — можно писать и простые хендлеры.

## Слои

```
              ┌───────────────────────────────┐
              │   granian (uvicorn fallback)  │   ASGI-сервер
              └───────────────┬───────────────┘
                              │ scope/receive/send
              ┌───────────────▼───────────────┐
              │    Ferronit  (core/app.py)    │   Python-диспетчер: lifespan,
              └───────┬──────────────┬────────┘   WS, HTTP, ошибки
                      │              │
   ┌──────────────────▼───┐   ┌──────▼───────────────┐
   │   ferronit._core     │   │  middleware-цепочка   │
   │   radix-роутер       │   │  (use / apply(app))   │
   │   JSON / gzip / CORS │   └──────┬───────────────┘
   │   multipart          │          │
   └──────────────────────┘   ┌──────▼───────────────┐
                              │      handler         │
                              │  binding + DI        │
                              └──────┬───────────────┘
                                     │
          ┌──────────────────────────┼──────────────────────────┐
          │                          │                          │
   ┌──────▼──────┐          ┌────────▼────────┐          ┌──────▼──────┐
   │ ddd.py      │          │ data: rawdb /    │          │ contrib/*   │
   │ hexagonal.py│          │ rawmodel         │          │ cors, health│
   └─────────────┘          └────────┬────────┘          │ kafka, ...  │
                                     │                   └─────────────┘
                              ┌──────▼──────┐
                              │  драйверы   │
                              │   3 движка  │
                              └─────────────┘
```

## Компоненты

| Компонент | Модуль | Назначение |
|---|---|---|
| Приложение | `ferronit/core/app.py` | класс `Ferronit` — ASGI-диспетчер, lifespan, WS, HTTP |
| Rust-ядро | `ferronit._core` (`ferronit-rs/`) | radix-роутинг, разбор запроса, gzip, CORS, multipart |
| Request | `ferronit/core/request.py` | ленивое чтение тела, query/headers, загрузки, ошибки |
| Response | `ferronit/core/response.py` | `JSONResponse`, `TextResponse`, `StreamingResponse`, `Response` |
| Роутинг | `ferronit._core` (Rust) | дерево маршрутов HTTP/WS, поиск по radix-дереву |
| Binding | `ferronit/binding.py` | привязка параметров хендлера, query/body-модели, pre-bind DI |
| DI | `ferronit/di.py` | `Container` (singleton/factory), `Annotated`-ключи |
| Middleware | `ferronit/middleware.py` | цепочка middleware, расширения `apply(app)` |
| DDD | `ferronit/ddd.py` | Command/Query, шины, агрегаты, события, Repository |
| Гексагон | `ferronit/hexagonal.py` | Port/Adapter, UnitOfWork, ApplicationService |
| Данные | `ferronit/contrib/sql.py`, `drivers.py`, `rawdb.py`, `rawmodel.py` | дерево SQL и диалекты, драйверы (asyncpg/aiomysql/aiosqlite), Pool/RawRepository, Model/select/Relation |
| Contrib | `ferronit/contrib/*` | cors, health, kafka, llm, ratelimit, security, staticfiles, tracing, vectordb |
| Конфиг | `ferronit/config.py` | `EnvConfig` |
| CLI | `ferronit/cli.py` | `new` / `dev` / `run` |
| OpenAPI | `ferronit/openapi.py` | автогенерация схемы по роутам |
| WebSocket | `ferronit/websocket.py` | WS-эндпоинты, Origin-guard |

Публичное API пакета (см. `ferronit/__init__.py`): `Ferronit`, `Header`, `Query`,
`Body`, `Request`, `Response`, `JSONResponse`, `TextResponse`, `StreamingResponse`,
`UploadedFile`, `WebSocket`, `WebSocketState`.

## Жизненный цикл HTTP-запроса

1. ASGI-сервер вызывает `Ferronit.__call__(scope, receive, send)`.
2. **Lifespan** (`_run_lifespan`): setup до `yield`, teardown после; на старте
   вызывается `_flush_deferred()` — DI-зависимости хендлеров резолвятся **один
   раз** и pre-bind, а не на каждый запрос.
3. **Маршрутизация в Rust**: `self._app.resolve(method, path)` по radix-дереву —
   без Python и regex. Нет роута → `404`.
4. **CORS preflight в Rust**: `OPTIONS` с валидным Origin обслуживается без
   входа в Python (`204` + заголовки).
5. **Тело не читается заранее** — в `Request` прокидывается только
   `max_body_size`; чтение ленивое (по `await req.json()` и т.п.).
6. **Middleware**: хендлер оборачивается цепочкой один раз и кэшируется
   (`_mw_cache`).
7. **Выполнение** хендлера (sync или async), результат нормализуется
   `_to_response()` (dict/Response/кортеж `(body, status)`).
8. **CORS-заголовок ответа** и **X-Trace-Id** добавляются после нормализации —
   выживают при любом типе возврата.
9. **gzip в Rust** с учётом q-факторов `Accept-Encoding` (`gzip;q=0` = не сжимать).
10. `resp._send(send)` — отправка ASGI-сообщений.

**Маппинг ошибок**: `PathParamError` → 404, `RequestError` → 400,
`UnsupportedMediaType` → 415, `BodyTooLarge` → 413, прочее → 500
(при `debug=True` исключение пробрасывается как есть).

## Rust-ядро (`ferronit._core`)

Собирается maturin-ом из `ferronit-rs/` (крейт с `abi3-py312` — один wheel на
все CPython 3.12+). Публичная поверхность: `FerronitApp`, `Request`, `Response`,
`Router`. Стабы типов — `ferronit/_core.pyi`.

| Задача | Реализация |
|---|---|
| Маршрутизация | radix-дерево `matchit` — O(1), без regex |
| Разбор запроса | method/path/query/headers нативно |
| gzip | сжатие в Rust, q-факторы |
| CORS | preflight и origin в Rust |
| Multipart | нативный парсер form-data |

Подробно — [rust-core.md](rust-core.md).

## DDD, DI и гексагональная

Ferronit поставляет строительные блоки, чтобы доменная логика жила отдельно от
HTTP и БД. Это опционально: минимальное приложение — один `@app.route`.

- **`ferronit.ddd`** — `Command`/`Query` (оба `msgspec.Struct`, поэтому сообщения
  сериализуемы и годятся для внешней шины), `CommandBus`/`QueryBus` (диспетчеризация
  по конкретному типу), `AggregateRoot` (буферизует `DomainEvent` через
  `record()`/`pull_events()`), `DomainEvent`, `Repository`.
- **`ferronit.hexagonal`** — `Port`/`Adapter`, стандартные порты (`Logger`,
  `Cache`, `MessageBus`, `EventBus`), `UnitOfWork` (транзакционная граница
  `async with uow.transaction()`), `ApplicationService`.
- **DI** — `Container` с `singleton()`/`factory()`; factory резолвит зависимости
  конструктора рекурсивно по type-аннотациям (PEP 563 обрабатывается). Несколько
  экземпляров одного типа различаются через `Annotated[Pool, "shop"]`.
- **Lifespan** — одна async-генераторная функция (`@app.lifespan`): ресурсы
  (драйвер БД и т.п.) создаются до `yield`, кладутся в `app.container`, на
  shutdown закрываются.
- **Binding** — параметры хендлера привязываются автоматически: path/query/
  headers/`Request`, а DI-параметры pre-bind на старте. Подробно —
  [binding.md](binding.md).

Примеры и разбор — в [ddd.md](ddd.md) и [binding.md](binding.md).

## Слой данных — rawdb + rawmodel

Единственный слой данных — `rawdb` + `rawmodel`, и он работает на трёх движках:
PostgreSQL (asyncpg), MySQL (aiomysql), SQLite (aiosqlite). Драйверы — необязательные
extras (`ferronit[postgres]`, `[mysql]`, `[sqlite]`) и импортируются при создании
пула, так что без них модули слоя импортируются. Никакого ORM — явный SQL,
bound-параметры, максимум throughput.

| Модуль | Что даёт |
|---|---|
| `contrib.rawdb` | `Pool`, `create_pool`, `RawUnitOfWork`, `RawRepository`, `Condition` — сырые dict-строки поверх драйвера |
| `contrib.rawmodel` | `Model` (неизменяемый `msgspec.Struct`), `Query`/`select`, `Relation`, `RawModelRepository`, `IdentityMap` |

- `pool.uow(readonly=True)` для чтений — пропускает `BEGIN`, экономит round-trip.
- `uow.model(User)` — типизированный доступ к строкам.
- `RawModelRepository` маппит строку **позиционно** (`cls(*row)`) без словаря —
  дешевле dict в ~1.85–1.9× (см. `bench/README.md`).

Подробно — [data/overview.md](data/overview.md) и [data/rawdb.md](data/rawdb.md).

## Contrib

Готовые модули поверх ядра: `cors`, `health`, `kafka` (MessageBus-адаптер),
`llm` (LLM-порты/адаптеры), `ratelimit`, `security` (ASVS-совместимые проверки),
`staticfiles`, `tracing` (`X-Trace-Id`), `vectordb` (векторные порты/адаптеры).

## Конфигурация и CLI

- `EnvConfig(prefix="APP_")` — `get/int/bool/list` из переменных окружения.
- CLI: `ferronit new <name>` (генерация DDD-проекта), `ferronit dev` (reload),
  `ferronit run` (granian, fallback на uvicorn), `--workers N`.

## Ключевые решения (ADR-стиль)

| Решение | Причина |
|---|---|
| Горячий путь в Rust | radix-роутинг O(1), gzip/CORS/multipart без Python на запрос |
| JSON | msgspec C fast-path (dict/list/Struct и `JSONResponse`), fallback stdlib `json` (dataclass/rawmodel); Rust-писатель выпилен |
| Явный SQL без ORM | rawdb/rawmodel на драйвере (asyncpg/aiomysql/aiosqlite): максимум throughput, предсказуемость, без ORM-накладных |
| Позиционный маппинг rawmodel | `cls(*row)` дешевле dict-маппинга |
| granian как сервер | ~4× быстрее uvicorn на этом фреймворке; uvicorn — fallback |
| Pre-bind DI на старте | зависимости резолвятся один раз, а не на каждый запрос |
| `abi3-py312` | один нативный wheel на все CPython 3.12+, цена ~1% throughput |

## Навигация по докам

| Тема | Док |
|---|---|
| Начало работы | [getting-started.md](getting-started.md) |
| DDD, DI, гексагональная | [ddd.md](ddd.md) |
| Маршрутизация и хендлеры | [routing.md](routing.md) |
| Привязка параметров | [binding.md](binding.md) |
| Request / Response | [requests.md](requests.md), [responses.md](responses.md) |
| Middleware и contrib | [middleware.md](middleware.md) |
| Логирование | [logging.md](logging.md) |
| Rust-ядро | [rust-core.md](rust-core.md) |
| Слой данных | [data/overview.md](data/overview.md), [data/rawdb.md](data/rawdb.md) |
| OpenAPI | [openapi.md](openapi.md) |
| WebSocket | [websocket.md](websocket.md) |
| Безопасность (ASVS) | [security/](security/) |
| Бенчмарки | [benchmarks.md](benchmarks.md) |
