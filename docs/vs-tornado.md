# Ferronit и Tornado: один сервис, N реализаций

Тот же контракт, что и в [сравнении с Django](vs-django.md) и
[с BlackSheep](vs-blacksheep.md): шесть эндпоинтов над таблицей `bench_rows`
(PostgreSQL). Tornado — классический Python-фреймворк (2009, FriendFeed/Facebook)
с собственным event loop: **он не ASGI**, поэтому не поднимается на granian/uvicorn,
а работает на своём `HTTPServer` в одном процессе. Это ключевое отличие от остальных
сравнений: цифры Tornado ниже включают **его собственный pure-Python сервер**, а не
granian.

| Вариант | Где лежит | Модель исполнения |
|---|---|---|
| **T1** — Tornado, идиоматичный | `bench/tornado_bench/app.py` — 1 файл | `RequestHandler` + `async def`, сырой asyncpg, свой `HTTPServer` |
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
| T1 — Tornado идиоматичный | 1 | 107 | **76** |
| B — Ferronit: агрегат + `Repository` | 1 | 152 | **69** |
| C — Ferronit: + CQRS | 1 | 218 | **128** |

Tornado компактен (76 значимых строк — рядом с Ferronit B, 69): `async def
get/post/put/delete` работают прямо в `RequestHandler`, без bridge-обвязки (в отличие
от Twisted, см. [vs-twisted.md](vs-twisted.md)). Ручной `json.loads` и `int()` в query
не добавляют строк — их просто нет как отдельного слоя (см. матрицу ниже).

## Вариант T1 — идиоматичный Tornado

```python
class RowsHandler(tornado.web.RequestHandler):
    async def get(self):
        limit = int(self.get_query_argument("limit", "100"))
        pool = await get_pool()
        async with pool.acquire() as conn:
            records = await conn.fetch(
                "SELECT id, name, email FROM bench_rows LIMIT $1", limit)
        self.write({"rows": [dict(r) for r in records]})

    async def post(self):
        data = json.loads(self.request.body)
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO bench_rows (name, email) VALUES ($1, $2) RETURNING id",
                data["name"], data["email"])
        self.write({"id": row["id"]})


def make_app():
    return tornado.web.Application([
        (r"/ping", PingHandler),
        (r"/rows", RowsHandler),
        (r"/rows/upsert", UpsertHandler),
        (r"/rows/([0-9]+)", RowHandler),
    ])


async def main():
    make_app().listen(8246)
    await asyncio.Event().wait()
```

Что Tornado делает сам: `self.write(dict)` сериализует ответ в JSON и ставит
`Content-Type: application/json`; маршрут — regex, путь `{id}` не типизируется
(`int()` вручную); `get_query_argument` разбирает query. Больше ничего: валидации,
DI и ORM нет — всё остальное сырой asyncpg.

**Проверено прогоном (tornado 6.5.10, CPython 3.14.4):** `async def`-хендлеры —
нативные корутины на asyncio-цикле (`IOLoop.current()`), пул asyncpg создаётся
лениво по первому запросу в том же цикле. Несовпавший путь (`/rows/abc` не бьётся
в `([0-9]+)`) → **404**; любое необработанное исключение в хендлере → **500**.

## Что Tornado даёт из коробки

| Возможность | Tornado 6.5.10 | Ferronit 0.17.0 |
|---|---|---|
| WebSocket | `tornado.websocket` (нативно, зрелый) | `WebSocket`-хендлеры |
| Async HTTP-клиент | `AsyncHTTPClient` | нет |
| Шаблоны / статика | `tornado.template`, `StaticFileHandler` | `contrib/staticfiles.py` |
| XSRF | `xsrf_cookies` | нет (вручную) |
| Типизация входа | нет (ручной `json.loads`/`int()`) | `msgspec.Struct` + `Annotated`, ошибка → **400** |
| DI | нет | `Container`: автосборка графа по аннотациям |
| OpenAPI | нет | `openapi.py` из type hints |
| ORM / миграции / админка | нет | нет |
| CORS / gzip | нет (third-party) / нет | `contrib/cors.py`, gzip в Rust-ядре |

Tornado силён не «батарейками» для HTTP-API, а долгоживущими соединениями и
событийной моделью; для типизированного REST-сервиса почти всё (валидация, DI,
схема) приходится брать руками или из сторонних пакетов.

## Поведение на некорректном вводе

Реальные коды ответов (Ferronit — вариант B):

| Запрос | T1 Tornado | Ferronit B |
|---|---|---|
| `PUT /rows/abc` (не `int`) | 404 | 404 |
| `POST /rows` с битым JSON | **500** | 400 |
| `POST /rows` без поля `name` | **500** | 200 (`name=""` — default) |
| `POST /rows` с `{"name": 1}` | **500** | 400 |
| `GET /rows?limit=abc` | **500** | 400 |
| `GET /rows?limit=-1` | **500** | 500 |
| `GET /rows?limit=0` | 200 (пустой список) | 200 (пустой список) |

Валидационного слоя у Tornado нет: всё, что не бьётся в regex-маршрут, даёт 404,
а любая ошибка в теле/query (`json.loads`, `int()`, asyncpg) падает 500-й.
`limit=-1` доходит до asyncpg и падает уже там (`InvalidRowCountInLimitClauseError:
LIMIT must not be negative`). Ferronit перехватывает тело/query на входе и отвечает
коротким **400**.

## Асинхронность

Tornado async-first с рождения: IOLoop поверх asyncio, `async def`-хендлеры
выполняются в цикле событий. Sync-хендлер Tornado тоже выполняется в цикле —
блокирующий код внутри него остановит все остальные запросы (аналог BlackSheep).
Ferronit async-only: sync-хендлера нет, заблокировать цикл нечем.

## Производительность

Свежий прогон, одна машина, `ab -c 50 -k`, лучшее из 3, таблица `bench_rows` —
1000 строк. **Tornado мерялся на своём `HTTPServer` (не ASGI → не granian), Ferronit B —
на granian (1 воркер):** числа включают сервер, а не только фреймворк.

| Вариант | `GET /ping` | `GET /rows?limit=100` | `GET /rows?limit=1000` |
|---|---|---|---|
| T1 — Tornado (свой сервер) | 9 937 | 3 371 | 1 017 |
| B — Ferronit (granian) | **100 792** | 7 160 | 1 437 |
| Отношение B / T1 | ×10.1 | ×2.1 | ×1.4 |

Как это читать:

- **На `ping` разрыв ×10 — это в основном сервер, а не роутинг.** Tornado отвечает
  pure-Python `HTTPServer`'ом (парсинг и запись ответа в Python), Ferronit — granian
  (Rust) + Rust-ядро роутинга/gzip. Tornado нельзя поднять на granian, поэтому
  «чистого» сравнения роутеров здесь нет.
- **На строках разрыв схлопывается в ×1.4–2.1** — потому что начинает доминировать
  слой данных (asyncpg), а не HTTP-сервер. Это ровно тот же вывод, что в остальных
  сравнениях: на чтениях решает слой данных, не фреймворк.
- Практический смысл: если сервис реально ходит в БД, разница Tornado → Ferronit
  на практике ближе к ×2, чем к ×10.

## Когда что выбирать

- **Tornado** — долгоживущие соединения: чаты, стримы, SSE, WebSocket на одном
  процессе; асинхронный HTTP-клиент и агенты; легаси-сервисы, уже написанные на
  Tornado. Для чистого REST-API быстрым он не будет (свой сервер).
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

# 2. T1 — Tornado (свой сервер, :8246)
.venv/bin/python bench/tornado_bench/app.py &
curl -s localhost:8246/ping
curl -s "localhost:8246/rows?limit=3"
curl -s -X POST localhost:8246/rows -H 'content-type: application/json' -d '{"name":"ada","email":"a@x"}'

# 3. B — Ferronit (granian, :8321)
.venv/bin/python -m granian --interface asgi --no-ws --host 127.0.0.1 --port 8321 \
  --workers 1 --log-level warning django_bench.ferronit_service:app --working-dir bench

# 4. Замеры (только ab)
ab -n 20000 -c 50 -k localhost:8246/ping
ab -n 20000 -c 50 -k "localhost:8246/rows?limit=100"
ab -n 10000 -c 50 -k "localhost:8246/rows?limit=1000"

# 5. Объём кода
.venv/bin/python bench/loc_compare.py
```

Смежные доки: [Ferronit и Django](vs-django.md), [Ferronit и FastAPI](vs-fastapi.md),
[Ferronit и BlackSheep](vs-blacksheep.md), [Ferronit и Litestar](vs-litestar.md),
[Ferronit и Twisted](vs-twisted.md), [бенчмарки](benchmarks.md).
