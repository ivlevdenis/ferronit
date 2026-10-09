# Маршрутизация и хендлеры

## Регистрация роутов

```python
from ferrox import Ferrox

app = Ferrox()

@app.route("/ping")
async def ping():
    return {"ok": True}

@app.route("/users/{user_id}")
async def get_user(user_id: int):
    return {"id": user_id}

@app.route("/users", methods=["POST"])
async def create_user():
    return {"created": True}
```

`@app.route(path, methods=None, **meta)`:

- `path` — шаблон маршрута; path-параметры пишутся как `{name}`.
- `methods` — список HTTP-методов, по умолчанию `["GET"]`.
- дополнительные именованные аргументы (`summary=`, `tags=`, ...) передаются в
  генератор OpenAPI.

Сама маршрутизация выполняется в **Rust** (radix-дерево `matchit`) — O(1)
поиск, без per-request накладных расходов Python и без деградации на тысячах
роутов.

## Хендлеры

Хендлеры могут быть `async def` или обычным `def`. Хендлер без type-hints с
единственным параметром `Request` идёт по быстрому пути без обёртки.

## Возвращаемые значения

Работает любое из перечисленного — фреймворк нормализует результат:

```python
@app.route("/a")
async def a():
    return {"ok": True}                    # dict → JSON, 200

@app.route("/b")
async def b():
    return ["x", "y"]                      # list → JSON, 200

@app.route("/c")
async def c():
    return {"error": "not found"}, 404     # кортеж (payload, status)

@app.route("/d")
async def d():
    return "hello"                         # str → text/plain

@app.route("/e")
async def e():
    return Response(b"raw", content_type="application/octet-stream")

@app.route("/f")
async def f():
    return SomeModel(name="a")             # msgspec/dataclass/pydantic → JSON

@app.route("/g")
async def g():
    async def stream():
        for i in range(5):
            yield f"data: {i}\n\n"
    return stream()                        # async-итератор → streaming
```

Всё остальное отправляется как `text/plain` через `str(result)`.

## Ответы

```python
from ferrox import Response, TextResponse, JSONResponse, StreamingResponse
```

| Класс | Назначение |
|---|---|
| `Response(body, status=200, content_type="text/plain", headers=None)` | сырое тело |
| `TextResponse(text, status=200)` | UTF-8 текст |
| `JSONResponse(data, status=200, headers=None)` | JSON-сериализация |
| `StreamingResponse(iterator, status=200, content_type="text/event-stream")` | чанки / SSE |

Значения заголовков очищаются от CRLF/LF-инъекций перед отправкой.

## Path-параметры

Path-параметры объявляются в шаблоне и подставляются по имени (см.
[внедрение зависимостей](injection.md)). Параметр, не прошедший приведение типа —
например `/users/abc` для `user_id: int` — возвращает **404**, намеренно не
раскрывая существование роута (ASVS 2.1.1).

## Обработка ошибок

| Исключение | HTTP |
|---|---|
| `PathParamError` | 404 |
| `RequestError` | 400 |
| `UnsupportedMediaType` | 415 |
| `BodyTooLarge` | 413 |
| любое другое | 500 (пробрасывается при `debug=True`) |

Установите `debug=True`, чтобы видеть исключения при разработке:

```python
app = Ferrox(debug=True)
```
