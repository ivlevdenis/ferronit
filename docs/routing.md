# Маршрутизация и хендлеры

## Зачем

Маршрутизация — самая горячая часть каждого запроса. Ferronit делает её в
**Rust** (radix-дерево `matchit`): поиск по `method + path` фактически O(1) и
не деградирует на тысячах маршрутов. Для сравнения, FastAPI на 1000 маршрутов
теряет до ×4 внутри одной таблицы; Ferronit — нет. Это то, что отличает его от
типичных Python-фреймворков.

## Регистрация роутов

```python
from ferronit import Ferronit

app = Ferronit()

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
- дополнительные именованные аргументы (`summary=`, `tags=`, ...) уходят в генератор OpenAPI.

## Хендлеры

Хендлер может быть `async def` или обычным `def`. Синхронный хендлер без
type-hints, принимающий один `Request`, идёт по быстрому пути без обёртки —
если производительность критична, держите хендлер максимально простым.

## Возвращаемые значения

Фреймворк нормализует результат — работает любое из:

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

Кортеж `(payload, status)` — документированный способ задать статус. Всё
остальное отправляется как `text/plain` через `str(result)`.

## Path-параметры

Path-параметры объявляются в шаблоне и подставляются по имени (см.
[внедрение зависимостей](injection.md)). Значение, не прошедшее приведение
типа — например `/users/abc` для `user_id: int` — возвращает **404**, намеренно
не раскрывая существование роута.

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
app = Ferronit(debug=True)
```
