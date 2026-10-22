# Привязка параметров хендлера

## Зачем

Хендлеры объявляют параметры с type-hints, а фреймворк разрешает их **на этапе
регистрации**: для каждого параметра собирается коэрсер-замыкание и источник
(path/query/header/body). На каждый запрос остаётся только вызов готовой
функции — никакого `get_origin`/`isinstance` в рантайме. Именно поэтому
типизированные параметры почти бесплатны.

```python
@app.route("/search")
async def search(q: str, page: int = 1, per_page: int = 20):
    ...
```

Порядок разрешения скалярного параметра:

1. **path-параметр** с тем же именем (приоритет над query);
2. значение из **query-строки**;
3. объявленный **default**.

## Типизированные параметры

| Hint | Поведение |
|---|---|
| `str` (или неизвестный) | передаётся как есть |
| `int` / `float` | приведение; некорректное значение → 400 (query/header) или 404 (path) |
| `bool` | принимает `true/false/1/0/yes/no/on/off` (без учёта регистра) |
| `list[T]` | повторяющиеся значения, каждое приводится через `T` |
| `int \| str` (union) | члены пробуются в порядке объявления |
| `req` / `Request` | сам объект `Request` |

```python
@app.route("/report")
async def report(year: int, tags: list[str], draft: bool = False):
    ...
# /report?year=2025&tags=a&tags=b&draft=yes
```

## Заголовки

Помечайте параметр через `Header` и `typing.Annotated`:

```python
from typing import Annotated
from ferronit import Header

@app.route("/whoami")
async def whoami(user_agent: Annotated[str, Header("User-Agent")]):
    return {"ua": user_agent}

@app.route("/api")
async def api(x_api_key: Annotated[str, Header()] = ""):
    # Header() → имя заголовка = имени параметра ("x-api-key")
    ...
```

## Body-модели

Параметр, чей hint — модель тела запроса (`msgspec.Struct`, dataclass или
Pydantic), декодируется из JSON-тела через `await req.model(...)`.
**Body-параметры требуют async-хендлера.**

```python
class NewUser(msgspec.Struct):
    name: str
    email: str

@app.route("/users", methods=["POST"])
async def create_user(user: NewUser):
    return {"id": 1, "name": user.name}
```

Модель без маркера — body; явно её помечают `Body()`:

```python
async def create_user(user: Annotated[NewUser, Body()]):
    ...
```

## Query-модели

Параметр с маркером `Query()` — модель `msgspec.Struct`, чьи поля маппятся на
query-параметры. Поля поддерживают констрейнты `msgspec.Meta`
(`ge`/`le`/`min_length`/`max_length`/`pattern` и т.д.).

```python
from ferronit import Query

class SearchQuery(msgspec.Struct):
    q: str = ""
    page: Annotated[int, msgspec.Meta(ge=1)] = 1
    per_page: Annotated[int, msgspec.Meta(ge=1, le=100)] = 20
    tags: Annotated[list[str], msgspec.Meta(max_length=5)] = []

@app.route("/search")
async def search(query: Annotated[SearchQuery, Query()]):
    return {"page": query.page, "tags": query.tags}
```

Валидация — **поле за полем**, мягкая: невалидное значение (неверный тип или
нарушенный констрейнт) откатывается к `default` поля; если default нет —
`RequestError` → **400**. Пример: `?page=0&per_page=500` → `page=1, per_page=20`
(оба откатились в default), а `?tags=a&...` из 6 элементов — `[]`.

Query-модель резолвится синхронно (не требует async-хендлера), в отличие от body.

## Зависимости из контейнера

Параметр, чей тип (или `Annotated`-ключ) зарегистрирован в `app.container`,
резолвится из контейнера, а не из запроса — хендлер получает готовый порт:

```python
from ferronit import Ferronit
from ferronit.di import Container

app = Ferronit(container=Container())
app.container.factory(UserRepository)

@app.route("/users/{id}")
async def get_user(req, users: UserRepository):
    return await users.get(req.params["id"])
```

Порядок разрешения параметра:

1. `req` / `Request` — сам объект запроса;
2. `Annotated[T, Header(...)]` — заголовок;
3. `Annotated[T, Query()]` — query-модель (`msgspec.Struct` из query-параметров);
4. `Annotated[T, Body()]` или модель тела — `req.model(...)`;
5. тип или `Annotated`-ключ из контейнера — `container.get(...)`;
6. иначе — path/query-параметр.

DI-зависимость резолвится на каждом запросе, поэтому синглтоны (пулы, клиенты)
должны быть зарегистрированы до первого запроса — обычно в `@app.lifespan`
(см. [архитектуру](architecture.md)).

## Missing против invalid

- **Missing** параметр без default → `ValidationError` (код `missing`, `field` —
  имя параметра) → **400** (path → **404**).
- **Invalid** значение (например `page=abc` для `int`) → `ValidationError`
  (код `invalid_type`) → **400** (path → **404**).
- Пустая строка `""` откатывается к default, если он объявлен.
- **Query-модель**: невалидное поле (тип или констрейнт `msgspec.Meta`)
  откатывается к default; без default — **400**, причём ошибки собираются все
  сразу (`too_small`, `too_large`, `pattern_mismatch`… — см.
  [коды ошибок](errors.md)).
