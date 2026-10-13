# Ferronit и Twisted: один сервис, N реализаций

Тот же контракт, что и в [сравнении с Django](vs-django.md) и
[с Tornado](vs-tornado.md): шесть эндпоинтов над таблицей `bench_rows`
(PostgreSQL). Twisted — старейший Python-фреймворк (2002) с собственным
reactor-циклом и экосистемой протоколов: **он не ASGI**, поэтому не поднимается
на granian/uvicorn, а работает на своём `reactor.listenTCP` в одном процессе.
Цифры Twisted ниже включают **его собственный pure-Python сервер**, а не granian.

| Вариант | Где лежит | Модель исполнения |
|---|---|---|
| **TW1** — Twisted, идиоматичный | `bench/twisted_bench/app.py` — 1 файл | `Resource` + `Deferred.fromFuture`, сырой asyncpg, свой reactor |
| **B** — Ferronit: агрегат + `Repository` | `bench/django_bench/ferronit_service.py` | Mapper/Repository/UoW, granian |
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
| TW1 — Twisted идиоматичный | 1 | 167 | **117** |
| B — Ferronit: агрегат + `Repository` | 1 | 152 | **69** |
| C — Ferronit: + CQRS | 1 | 218 | **128** |

Twisted — самый многословный из идиоматичных вариантов (117 значимых строк; длиннее
только CQRS-вариант C — 128): ради asyncpg приходится ставить `asyncioreactor` и
писать bridge (`Deferred.fromFuture(asyncio.ensure_future(...))`) плюс на каждый
хендлер — пару `render_METHOD` + `async def _method`. Это цена «старого»
Deferred-мира за запуск asyncio-библиотеки.

## Вариант TW1 — идиоматичный Twisted

```python
from twisted.internet import asyncioreactor
asyncioreactor.install()                       # ДО импорта reactor
from twisted.internet import reactor
from twisted.internet.defer import Deferred
from twisted.web import pages, resource, server
from twisted.web.server import NOT_DONE_YET


class BaseResource(resource.Resource):
    def _run(self, coro, request):
        d = Deferred.fromFuture(asyncio.ensure_future(coro))
        d.addErrback(self._error, request)
        return NOT_DONE_YET

    def _json(self, request, obj):
        request.setHeader(b"content-type", b"application/json")
        request.write(json.dumps(obj).encode())
        request.finish()


class RowsResource(BaseResource):
    def getChild(self, path, request):
        if path == b"upsert":
            return UpsertResource()
        if path.isdigit():
            return RowResource(int(path))
        return pages.notFound()

    def render_GET(self, request):
        return self._run(self._get(request), request)

    async def _get(self, request):
        limit = int(request.args.get(b"limit", [b"100"])[0])
        pool = await get_pool()
        async with pool.acquire() as conn:
            records = await conn.fetch(
                "SELECT id, name, email FROM bench_rows LIMIT $1", limit)
        self._json(request, {"rows": [dict(r) for r in records]})
```

Что Twisted делает сам: дерево `Resource` с `getChild` для path-параметров,
`render_GET`/`render_POST`/`render_PUT`/`render_DELETE`, 404 через `pages.notFound()`.
Всё остальное — руками: JSON-разбор (`request.content.read()` + `json.loads`),
сериализация ответа, и мост к asyncio для asyncpg.

**Проверено прогоном (twisted 26.4.0, CPython 3.14.4):** `defer.ensureDeferred`
для asyncpg **не годится** — он гонит корутину через `_inlineCallbacks`, внутри неё
`asyncio.get_running_loop()` возвращает `None`, и asyncpg падает
`RuntimeError: no running event loop`. Работает только
`Deferred.fromFuture(asyncio.ensure_future(coro))` — корутина становится настоящей
asyncio-задачей на цикле реактора. `resource.NoResource()` в 26.x помечен deprecated,
современная замена — `pages.notFound()` (это **функция**, вызывать с `()`).

## Что Twisted даёт из коробки

| Возможность | Twisted 26.4.0 | Ferronit 0.17.0 |
|---|---|---|
| Сетевая экосистема | reactor + протоколы TCP/UDP/SSL/SSH/DNS/IRC… | нет |
| Web | `Resource`-дерево, статика, WSGI | роутер + Rust-ядро |
| Async-примитив | `Deferred` (родоначальник Future) | asyncio |
| Мост к asyncio | `asyncioreactor` + `Deferred.fromFuture` | — (сам asyncio) |
| Типизация входа | нет (ручной `json.loads`/`int()`) | `msgspec.Struct` + `Annotated`, ошибка → **400** |
| DI | нет | `Container`: автосборка графа по аннотациям |
| OpenAPI | нет | `openapi.py` из type hints |
| ORM / миграции / админка | нет | нет |
| WebSocket | нет (только сторонние) | `WebSocket`-хендлеры |

Twisted силён не HTTP-API, а общими сетевыми протоколами и Deferred-кодовой базой;
для типизированного REST-сервиса почти всё приходится писать руками.

## Поведение на некорректном вводе

Реальные коды ответов (Ferronit — вариант B):

| Запрос | TW1 Twisted | Ferronit B |
|---|---|---|
| `PUT /rows/abc` (не `int`) | 404 | 404 |
| `POST /rows` с битым JSON | **500** | 400 |
| `POST /rows` без поля `name` | **500** | 200 (`name=""` — default) |
| `POST /rows` с `{"name": 1}` | **500** | 400 |
| `GET /rows?limit=abc` | **500** | 400 |
| `GET /rows?limit=-1` | **500** | 500 |
| `GET /rows?limit=0` | 200 (пустой список) | 200 (пустой список) |

Валидационного слоя нет: несовпавший путь даёт 404 (`pages.notFound()`), а любая
ошибка в теле/query ловится errback'ом в `_run` и отвечает 500-й. `limit=-1`
доходит до asyncpg и падает уже там (`InvalidRowCountInLimitClauseError`). Ferronit
перехватывает тело/query на входе и отвечает коротким **400**.

## Асинхронность

Twisted async-first с 2002 года, но на своём reactor'е; asyncio-библиотеки
подключаются мостом `asyncioreactor` + `Deferred.fromFuture`. Sync-хендлер в
`Resource` (без `render_METHOD`-обвязки) выполняется в цикле реактора — блокирующий
код останавливает все запросы. Ferronit async-only, sync-хендлера нет.

## Производительность

Свежий прогон, одна машина, `ab -c 50 -k`, лучшее из 3, таблица `bench_rows` —
1000 строк. **Twisted мерялся на своём reactor-сервере (не ASGI → не granian),
Ferronit B — на granian (1 воркер):** числа включают сервер, а не только фреймворк.

| Вариант | `GET /ping` | `GET /rows?limit=100` | `GET /rows?limit=1000` |
|---|---|---|---|
| TW1 — Twisted (свой сервер) | 10 846 | 3 605 | 1 045 |
| B — Ferronit (granian) | **100 792** | 7 160 | 1 437 |
| Отношение B / TW1 | ×9.3 | ×2.0 | ×1.4 |

Как это читать:

- **На `ping` разрыв ×9.3 — это в основном сервер, а не роутинг.** Twisted отвечает
  pure-Python `twisted.web`-сервером, Ferronit — granian (Rust) + Rust-ядро. Twisted
  нельзя поднять на granian, «чистого» сравнения роутеров здесь нет. (Twisted и
  Tornado на своих серверах дают почти одинаковые ~10k — оба pure-Python.)
- **На строках разрыв схлопывается в ×1.4–2.0** — доминирует слой данных (asyncpg).
  Тот же вывод, что везде в этой серии: на чтениях решает слой данных, не фреймворк.
- Практический смысл: для сервиса, который ходит в БД, разница Twisted → Ferronit
  на практике ближе к ×2, чем к ×9.

## Когда что выбирать

- **Twisted** — если нужна широкая сетевая экосистема не только HTTP (TCP/UDP/SSL/
  SSH/DNS), протоколы собственного дизайна или уже есть Deferred-кодовая база.
  Для чистого REST-API быстрым он не будет (свой pure-Python сервер).
- **Ferronit** — HTTP-сервис на максимуме req/s (granian + Rust-ядро), DDD/CQRS/DI,
  типизированный вход и OpenAPI из коробки.
- **Ни то, ни другое** — если нужен ORM/админка/миграции: это [Django](vs-django.md).

## Как воспроизвести

```bash
# 1. PostgreSQL + таблица 1000 строк
docker start ferronit-pg || docker run -d --name ferronit-pg \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_USER=postgres -e POSTGRES_DB=postgres \
  -p 5432:5432 postgres:18
.venv/bin/python bench/api_bench.py            # seed(): CREATE TABLE bench_rows + 1000 строк

# 2. TW1 — Twisted (свой reactor-сервер, :8247)
.venv/bin/python bench/twisted_bench/app.py &
curl -s localhost:8247/ping
curl -s "localhost:8247/rows?limit=3"
curl -s -X POST localhost:8247/rows -H 'content-type: application/json' -d '{"name":"ada","email":"a@x"}'

# 3. B — Ferronit (granian, :8321)
.venv/bin/python -m granian --interface asgi --no-ws --host 127.0.0.1 --port 8321 \
  --workers 1 --log-level warning django_bench.ferronit_service:app --working-dir bench

# 4. Замеры (только ab)
ab -n 20000 -c 50 -k localhost:8247/ping
ab -n 20000 -c 50 -k "localhost:8247/rows?limit=100"
ab -n 10000 -c 50 -k "localhost:8247/rows?limit=1000"

# 5. Объём кода
.venv/bin/python bench/loc_compare.py
```

Смежные доки: [Ferronit и Django](vs-django.md), [Ferronit и FastAPI](vs-fastapi.md),
[Ferronit и BlackSheep](vs-blacksheep.md), [Ferronit и Litestar](vs-litestar.md),
[Ferronit и Tornado](vs-tornado.md), [бенчмарки](benchmarks.md).
