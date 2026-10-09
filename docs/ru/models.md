# Модели и сериализация

## Body-модели

Для тел запросов поддерживаются три вида моделей:

1. **`msgspec.Struct`** — быстрый путь: декод из байтов тела одним C-проходом.
2. **Pydantic v2** — через зарегистрированный кодек.
3. **`@dataclass`** — fallback-кодек.

```python
import msgspec

class NewUser(msgspec.Struct):
    name: str
    email: str
    age: int = 0
```

Используйте как параметр хендлера (автодекод) или явно:

```python
@app.route("/users", methods=["POST"])
async def create_user(req):
    user = await req.model(NewUser)   # RequestError → 400 при ошибке валидации
    return {"name": user.name}
```

## Ответы

Возврат модели из хендлера автоматически сериализует её в JSON. Модели с
`model_dump` (Pydantic), `__dataclass_fields__`, `__columns__` (rawmodel) или
`__struct_fields__` (msgspec) распознаются все. msgspec и rawmodel-модели
сериализуются Rust-энкодером (serde_json), Pydantic — через собственный dump.

```python
@app.route("/me")
async def me():
    return User(id=1, name="Ada")     # msgspec.Struct → JSON в Rust
```

## Кастомные кодеки

`ferrox.contrib.pydantic` предоставляет подключаемый реестр кодеков:

```python
from ferrox.contrib.pydantic import register_codec, PydanticCodec

register_codec(MyType, MyCodec)   # decode_json / encode_json теперь знают MyType
```

Ядро остаётся без зависимостей: Pydantic импортируется только при использовании.
