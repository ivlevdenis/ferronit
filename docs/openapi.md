# OpenAPI

## Зачем

Схему OpenAPI не нужно писать руками — она выводится из type-hints хендлеров и
моделей. Держите сигнатуры типизированными, и `/openapi.json` всегда актуален.

Каждый зарегистрированный роут автоматически пополняет генератор OpenAPI 3.0.
`SchemaBuilder` выводит типы из сигнатур и моделей.

```python
app = Ferrox()

@app.route("/users/{user_id}", summary="Получить пользователя", tags=["users"])
async def get_user(user_id: int):
    return {"id": user_id}
```

Билдер `app.openapi` доступен для кастомизации:

```python
schema = app.openapi   # экземпляр OpenAPI (SchemaBuilder)
```

## Схемы моделей

`SchemaBuilder.from_model`:

- **Pydantic** → `model_json_schema()` (внутренние ключи вычищаются);
- **dataclass** → объект с полями (типизированы строками);
- **msgspec / rawmodel** → объект, чьи `__columns__` становятся string-свойствами.

```python
from ferrox.openapi import SchemaBuilder

fragment = SchemaBuilder.from_model(NewUser)
```

## Отдача документа

`OpenAPI` даёт `build()` (dict) и `json()` (строка). Подключите к роуту:

```python
@app.route("/openapi.json")
async def openapi_json():
    return Response(app.openapi.json(), content_type="application/json")
```

Метаданные роута (`summary`, `tags`, ...) передаются через именованные
аргументы `@app.route`. Аннотация возврата хендлера, если это модель,
сохраняется в `components/schemas` и ссылается из ответа `200`.
