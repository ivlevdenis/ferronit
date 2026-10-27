# Ferronit и Django: один сервис, три реализации

Сравнение не «фреймворков вообще», а одного и того же сервиса: REST API из шести
эндпоинтов над одной таблицей `bench_rows` (PostgreSQL). Все три варианта лежат в
репозитории (A и B — файлами, C — строкой в `bench/api_bench.py`), поднимаются одной
командой и отвечают одинаково.

| Вариант | Где лежит | Модель исполнения |
|---|---|---|
| **A** — Django ORM | `bench/django_bench/` — 5 файлов | sync-first |
| **B** — Ferronit: агрегат + `Repository` | `bench/django_bench/ferronit_service.py` — 1 файл | async, Mapper/Repository/UoW, роуты вызывают репозиторий |
| **C** — Ferronit: + CQRS | строка `FERRONIT_APP` в `bench/api_bench.py` — 1 файл | то же плюс CommandBus/QueryBus и хендлеры |

Одна и та же таблица (`DDL` — `bench/api_bench.py::seed`), один и тот же контракт:

| Метод и путь | Ответ |
|---|---|
| `GET /ping` | `{"ok": true}` |
| `GET /rows?limit=N` | `{"rows": [...]}` |
| `POST /rows` | `{"id": N}` |
| `PUT /rows/{id}` | `{"id": N}` |
| `DELETE /rows/{id}` | `{"ok": true}` |
| `POST /rows/upsert` | `{"id": N}` |

Django-версия и обе Ferronit-версии отдают одни и те же данные: `{"ok": true}`,
`{"rows": [{"id": 1, "name": "ada", "email": "a@x"}]}`, `{"id": 1}`, `{"ok": true}` —
различается только форматирование JSON (stdlib `json` против msgspec).

## Цена в строках

Подсчёт — AST-обход (`bench/loc_compare.py`): без пустых строк, комментариев и
докстрингов.

| Вариант | Файлов | Всего строк | Значимых |
|---|---|---|---|
| A — Django | 5 (+1 авто-миграция) | 91 | **66** |
| B — Ferronit: агрегат + `Repository` | 1 | 152 | **69** |
| C — Ferronit: + CQRS | 1 | 218 | **128** |

Ключевое: объём кода сопоставим (69 против 66), но Django получает миграцию
автоматически, а Ferronit пишет доменный слой руками. Разница B и C — 59 значимых
строк: это ровно обвязка CQRS (команды, запросы, шины, хендлеры), сам доступ к БД
в обоих вариантах — фреймворковый `Repository`.

## Вариант A — Django

`bench/django_bench/models.py` — схема описывается классом, миграцию генерирует
`makemigrations`:

```python
class BenchRow(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.TextField()
    email = models.TextField()

    class Meta:
        db_table = "bench_rows"
```

`bench/django_bench/views.py` — sync-хендлер, ORM прямо в представлении
(полный файл — 30 значимых строк):

```python
def rows(request):
    if request.method == "POST":
        data = _body(request)
        row = BenchRow.objects.create(name=data["name"], email=data["email"])
        return JsonResponse({"id": row.id})
    limit = int(request.GET.get("limit", "100"))
    data = list(BenchRow.objects.all()[:limit].values("id", "name", "email"))
    return JsonResponse({"rows": data})
```

Роутинг — `urls.py` (`path("rows/<int:row_id>", views.rows_item)`), точка входа —
`asgi.py` (4 строки), настройки — `settings.py` (17 значимых строк).

## Вариант B — Ferronit: агрегат + `Repository`

Доступ к БД — полностью фреймворковый: агрегат `Row`, `RowMapper` и
`RowsRepository(Repository[Row, BenchRow, RowMapper, ShopPool])`. Ни своего SQL, ни
самописного репозитория — только базовые `list/save/delete` и `uow.model()`.
Полный листинг — `bench/django_bench/ferronit_service.py`; ядро (33 строки,
объявления `DSN`, `Container`, `ShopPool`, `RowBody` опущены):

```python
class BenchRow(Model):
    __table__ = "bench_rows"
    id: int = 0
    name: str = ""
    email: str = ""

class Row(AggregateRoot):
    def __init__(self, row_id: int, name: str, email: str) -> None:
        super().__init__()
        self.id = row_id
        self.name = name
        self.email = email

    @classmethod
    def new(cls, name: str, email: str) -> "Row":
        return cls(0, name, email)

class RowMapper(Mapper[Row, BenchRow]):
    def to_aggregate(self, model: BenchRow) -> Row:
        return Row(model.id, model.name, model.email)

    def from_aggregate(self, aggregate: Row) -> BenchRow:
        return BenchRow(id=aggregate.id, name=aggregate.name, email=aggregate.email)

class RowsRepository(Repository[Row, BenchRow, RowMapper, ShopPool]):
    async def insert(self, name: str, email: str) -> Row:
        async with self.uow() as uow:
            saved = await uow.model(BenchRow).save(BenchRow(name=name, email=email))
        return self._mapper.to_aggregate(saved)

    def to_read_model(self, aggregate: Row) -> BenchRow:
        return self._mapper.from_aggregate(aggregate)

@app.route("/rows")
async def rows(repo: RowsRepository, limit: int = 100):
    aggregates = await repo.list(limit=limit)
    return {"rows": [repo.to_read_model(a) for a in aggregates]}

@app.route("/rows", methods=["POST"])
async def insert(body: Annotated[RowBody, Body()], repo: RowsRepository):
    return {"id": (await repo.insert(body.name, body.email)).id}
```

Что фреймворк делает сам:

- `Repository[A, M, MAP, P]` достаёт агрегат, модель, маппер и тип пула из generic
  (`__init_subclass__`) — репозиторий собирается как `RowsRepository(shop_pool)`,
  никаких ручных связываний;
- `repo.list(limit=N)` сам открывает `uow(readonly=True)` (без `BEGIN`) и переводит
  строки в агрегаты через `RowMapper.to_aggregate`;
- `repo.save(aggregate)`/`repo.delete(id)` — одна UoW на операцию, upsert заголовка
  и синхронизация дочерних таблиц; в этом сервисе дочерних нет, поэтому
  `insert`/`update` добавлены двумя короткими методами: INSERT должен вернуть
  выданный базой `id`, а базовый `save()` возвращает переданный агрегат;
- `repo: RowsRepository` — зависимость из DI-контейнера; `Annotated[RowBody, Body()]` —
  разбор и валидация JSON-тела (битое → 400); `row_id: int` — приведение и 404 на
  `/rows/abc`; `dict`/модель сериализуются без `JsonResponse`.

## Вариант C — Ferronit: + CQRS

`FERRONIT_APP` в `bench/api_bench.py` (128 значимых строк; при прогоне стенд пишет
его в `bench/_bench_api_ferronit.py` — файл генерируется и в git не хранится) берёт
тот же `Repository` и `Mapper`, что и вариант B, и добавляет поверх шины:
`InsertRow`/`UpdateRow`/`UpsertRow`/`DeleteRow` (`Command`) и `ListRows` (`Query`) —
простые `msgspec.Struct`, то есть команду можно отправить и во внешнюю шину.
Хендлеры подписываются в `@app.lifespan`, а роут становится тонким адаптером:

```python
@app.route("/rows", methods=["POST"])
async def insert(body: Annotated[InsertBody, Body()], commands: CommandBus):
    row = await commands.dispatch(InsertRow(body.name, body.email))
    return {"id": row.id}
```

Это +59 значимых строк к варианту B. Смысл появляется, когда бизнес-логика растёт:
правила живут в агрегате, а HTTP-слой их только вызывает. Для сравнения «в лоб»
достаточно варианта B.

## Что Django даёт из коробки, а Ferronit — нет

| Возможность | Django 6.1.2 | Ferronit 0.17.0 |
|---|---|---|
| Миграции схемы | `makemigrations`/`migrate` | **нет** — `rawdb` пишет: «deliberately has no … migration support»; DDL руками (или Alembic сбоку) |
| ORM и связи | есть, плюс `select_related`/`prefetch_related` | нет ORM: явный SQL; связи — `Relation` (1 доп. запрос на всех, N+1 не бывает) |
| Админка | `django.contrib.admin` | нет |
| Аутентификация, сессии, права | `contrib.auth` + `contrib.sessions` | нет (есть только `contrib/security.py` — security-заголовки) |
| Формы, CSRF, i18n, пагинация, сигналы | есть | нет |
| Management-команды | есть | нет (CLI `ferronit new/dev/run`) |
| OpenAPI | внешний пакет (drf-spectacular) | встроенный `openapi.py` из type hints |
| Валидация тел/параметров | Forms или внешний DRF | `msgspec.Struct` + `Meta` + `Annotated`, включая query-модели |
| DI | нет (импорты, `AppConfig`, фабрики) | `Container`: автосборка по аннотациям, синглтоны/фабрики, `Annotated`-ключи |
| Транзакции | `transaction.atomic`, `ATOMIC_REQUESTS` | `uow()`: одна транзакция на запрос, `readonly=True` пропускает `BEGIN` |
| ASGI lifespan | **не поддерживается** (`django/core/handlers/asgi.py`; granian: `ASGI Lifespan errored, continuing without Lifespan support`) | `@app.lifespan` — пулы и клиенты создаются в цикле событий сервера |
| Экосистема, найм, документация | крупнейшая в Python | молодая (0.17.0) |

DRF, Celery, Channels и прочее — внешние пакеты; в этом репозитории они не
установлены, поэтому сравнивать нечего: в Django-стенде используется голый
`JsonResponse` + Django ORM.

## Асинхронность

Ferronit async-only, поэтому Django-специфика тут принципиальна. Проверено на
`django 6.1.2` (probe-проект, sqlite):

- **sync ORM внутри `async def` запрещён**: `.create()`, `.filter().update()`,
  итерация `QuerySet` → `SynchronousOnlyOperation: You cannot call this from an
  async context - use a thread or sync_to_async`.
- **sync-представления под ASGI** Django оборачивает в
  `sync_to_async(..., thread_sensitive=False)` — то есть каждое sync-представление
  уходит в пул потоков. Отсюда измеренный потолок `GET /ping` — 1 988 req/s против
  95 119 у Ferronit (×48).
- **async ORM есть**, но частичный: `Model.asave/adelete/arefresh_from_db`, у
  менеджера/QuerySet — `acreate/aget/aupdate/adelete/acount/aget_or_create/
  aupdate_or_create/abulk_create/aiterator/aexists/afirst/alast/ain_bulk/aaggregate`;
  отдельного `afilter` нет — фильтр строится синхронно, `await` только на
  терминальной операции. Замеры строк из БД сняты на sync-ORM + sync-хендлере
  (Django-ветка стенда `django_bench`); отдельного прогона на async-ORM в стенде
  нет.

## Производительность

Методика и полные таблицы — [benchmarks.md](benchmarks.md): `ab -n … -c 50 -k`,
granian 1 воркер, лучший из прогонов, одна машина.

| Сценарий | Ferronit | Django | Отношение |
|---|---|---|---|
| `GET /ping` (dict) | 95 119 | 1 988 | ×48 |
| `GET /rows?limit=1000` | 1 179 | 382 | ×3.1 |
| INSERT одной строки | 5 702 | 456 | ×12 |
| UPDATE одной строки | 932 | 449 | ×2.1 |
| UPSERT | 984 | 336 | ×2.9 |
| DELETE | 7 095 | 429 | ×17 |

Разрыв на записях меньше, потому что там всех ограничивает коммит (`fsync`) и
`BEGIN`/`COMMIT`, а не фреймворк. На чистом HTTP и чтениях разрыв максимальный:
у Django добавляются поток на запрос, ORM-объект на строку и stdlib-`json`.

## Поведение на некорректном вводе

Один и тот же набор «плохих» запросов, реальные коды ответов (Ferronit — вариант B,
Django — стенд `django_bench`):

| Запрос | Ferronit | Django |
|---|---|---|
| `PUT /rows/abc` (не `int`) | 404 | 404 (HTML-страница) |
| `POST /rows` с битым JSON (`{oops`) | 400 | **500** |
| `POST /rows` без поля `name` | 200 (`name=""` — сработал default) | **500** (`KeyError`) |
| `POST /rows` с `{"name": 1}` | 400 | 500 |
| `GET /rows?limit=abc` | 400 | **500** |
| `GET /rows?limit=-1` | 500 (`PostgreSQL: LIMIT must not be negative`) | 500 (ORM: `Negative indexing is not supported`) |

Что это значит на практике: типизация параметров и тела в Ferronit — часть
объявления хендлера, поэтому «плохой ввод» отсекается фреймворком (400) до кода
(`404` для path-параметра). У «голого» Django валидация — отдельная работа
(Forms/DRF), без неё запрос доходит до `data["name"]` и падает 500-й. Мягкая
сторона msgspec-декодера: поле с `default` можно не прислать, а `limit=-1`
пропускается в SQL — диапазон задаётся констрейнтом
(`Annotated[int, msgspec.Meta(ge=1)]` в query-модели, см. [binding.md](binding.md)).

## Что придётся писать самому при переходе с Django на Ferronit

1. **Миграции** — Ferronit их не генерирует: DDL вручную или Alembic сбоку.
2. **Аутентификация, сессии, права** — писать порты/адаптеры (гексагональные
   `Port`/`Adapter` в поставке есть, реализаций — нет).
3. **Админка** — только своим UI; здесь разрыв в затратах максимальный.
4. **Фоновые задачи и расписание** — нет ни Celery-интеграции, ни команд.
5. **Шаблоны, i18n, формы** — нет; Ferronit ориентирован на JSON-API.

Взамен получаете: отсутствие ORM-слоя в горячем пути, единый `uow()` на запрос,
DI из коробки и производительность выше на всём измеренном профиле — от ×1.5 на
записях до ×48 на `ping` (таблица выше).

## Когда что выбирать

- **Django** — админка и бэк-офис нужны «сегодня», CRUD доминирует, команда
  junior-heavy, сроки важнее RPS, нужна готовая аутентификация и экосистема.
- **Ferronit** — узкий горячий API (JSON + SQL) под нагрузкой, замена Django-ручки,
  которая упирается в потолок; сложный домен (DDD/CQRS) и готовность писать
  инфраструктуру (миграции, auth) самим.

Смешанный вариант практичен: Django — админка, внутренние CRUD и фоновые задачи,
Ferronit — публичный API с нагрузкой.

## Как воспроизвести

```bash
# 1. PostgreSQL + таблица (та же, что в бенч-стенде)
docker start ferronit-pg || docker run -d --name ferronit-pg \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_USER=postgres -e POSTGRES_DB=postgres \
  -p 5432:5432 postgres:18
.venv/bin/python bench/api_bench.py   # seed(): CREATE TABLE bench_rows + 1000 строк

# 2. Вариант B — Ferronit
.venv/bin/python -m uvicorn django_bench.ferronit_service:app --port 8321 --app-dir bench
curl -s localhost:8321/ping
curl -s -X POST localhost:8321/rows -H 'content-type: application/json' \
     -d '{"name":"ada","email":"a@x"}'
curl -s "localhost:8321/rows?limit=3"

# 3. Вариант A — Django
cd bench && DJANGO_SETTINGS_MODULE=django_bench.settings \
  ../.venv/bin/python -m granian --interface asgi --no-ws django_bench.asgi:application --port 8322
curl -s localhost:8322/ping

# 4. Цифры: бенч-стенд целиком (req/s меряем только ab — Python-клиент меряет себя)
BENCH_APPS=ferronit,django .venv/bin/python bench/api_bench.py
./bench/run_all_ab.sh

# 5. Объём кода вариантов A/B/C
.venv/bin/python bench/loc_compare.py
```

Смежные доки: [слой данных](data/rawdb.md), [DDD и DI](ddd.md),
[бенчмарки](benchmarks.md), [FastAPI](vs-fastapi.md), [BlackSheep](vs-blacksheep.md),
[Litestar](vs-litestar.md), [Tornado](vs-tornado.md), [Twisted](vs-twisted.md),
[быстрый старт](getting-started.md).
