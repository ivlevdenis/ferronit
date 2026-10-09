# Внедрение зависимостей

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

## Missing против invalid

- **Missing** параметр без default → `RequestError` → **400** (path → **404**).
- **Invalid** значение (например `page=abc` для `int`) → `RequestError` → **400**
  (path → **404**).
- Пустая строка `""` откатывается к default, если он объявлен.
