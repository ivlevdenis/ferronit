# Ответы и сериализация

## Ответы

Фреймворк принимает из хендлера почти всё — dict/list/строку/модель/стрим
(полный список в [маршрутизации](routing.md)). Когда нужен контроль над
статусом, заголовками или телом — собирайте ответ явно:

```python
from ferronit import Response, TextResponse, JSONResponse, StreamingResponse
```

| Класс | Назначение |
|---|---|
| `Response(body, status=200, content_type="text/plain", headers=None)` | сырое тело |
| `TextResponse(text, status=200)` | UTF-8 текст |
| `JSONResponse(data, status=200, headers=None)` | JSON-сериализация |
| `StreamingResponse(iterator, status=200, content_type="text/event-stream")` | чанки / SSE |

Значения заголовков очищаются от CRLF/LF-инъекций перед отправкой.

## Модели в ответе

Возврат модели из хендлера автоматически сериализует её в JSON. Распознаются
модели с `model_dump` (Pydantic), `__dataclass_fields__`, `__columns__`
(rawmodel) или `__struct_fields__` (msgspec). msgspec и rawmodel-модели
сериализуются Rust-энкодером (serde_json), Pydantic — собственным dump.

```python
@app.route("/me")
async def me():
    return User(id=1, name="Ada")     # msgspec.Struct → JSON в Rust
```

## Какие модели поддерживаются

Для тел запросов и ответов — три вида моделей:

1. **`msgspec.Struct`** — быстрый путь: декод из байтов тела одним C-проходом,
   сериализация в Rust. Рекомендуемый вариант для горячих путей.
2. **Pydantic v2** — через зарегистрированный кодек (валидация богаче).
3. **`@dataclass`** — fallback-кодек без внешних зависимостей.

## Кастомные кодеки

`ferronit.contrib.pydantic` предоставляет подключаемый реестр кодеков:

```python
from ferronit.contrib.pydantic import register_codec, PydanticCodec

register_codec(MyType, MyCodec)   # decode_json / encode_json теперь знают MyType
```

Ядро остаётся без зависимостей: Pydantic импортируется только при
использовании.
