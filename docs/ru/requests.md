# Запросы

Объект `Request` оборачивает ASGI scope с **нативным Rust-парсингом**. Заголовки
и строка запроса разбираются лениво (при первом обращении) и кэшируются на
время жизни запроса.

```python
from ferrox import Ferrox

app = Ferrox()

@app.route("/inspect", methods=["GET"])
async def inspect(req):
    return {
        "method": req.method,
        "path": req.path,
        "params": req.params,
        "query": req.query,
        "headers": req.headers,
    }
```

## Атрибуты и методы

| Член | Тип | Примечания |
|---|---|---|
| `req.params` | `dict[str, str]` | path-параметры маршрута |
| `req.method` | `str` | например `"GET"` |
| `req.path` | `str` | путь без query-строки |
| `req.headers` | `dict[str, str]` | ключи в нижнем регистре, лениво |
| `req.get_header(name)` | `str \| None` | регистронезависимый одиночный поиск |
| `req.query` | `dict[str, list[str]]` | **каждое значение — список** (повторы сохраняются) |
| `req.cookies` | `dict[str, str]` | разобранный заголовок `Cookie` |
| `req.get_cookie(name, default=None)` | `str \| None` | одна cookie |

Значения query — всегда списки: `?a=1&a=2` → `{"a": ["1", "2"]}`. Для
типизированного скаляра используйте внедрение зависимостей (см.
[injection](injection.md)).

## Чтение тела

```python
body: bytes = await req.body()          # всё тело; BodyTooLarge при превышении лимита
data = await req.json()                 # разбор JSON; RequestError (→400) при ошибке
user = await req.model(UserModel)       # декод в msgspec/dataclass/pydantic
```

- `body()` стримит ASGI-чанки. Если приложение создано с `max_body_size`, при
  превышении лимита бросается `BodyTooLarge` → **413**.
- `model()` имеет быстрый путь для `msgspec.Struct` — декод из байтов одним
  C-проходом; Pydantic и dataclass идут через зарегистрированный кодек.

## Формы и файлы

```python
fields = await req.form()    # urlencoded → {name: [values]}; multipart → только текстовые поля
files = await req.files()    # multipart → {name: [UploadedFile]}
```

`UploadedFile`:

```python
f.filename       # исходное имя файла
f.content        # bytes
f.content_type   # content type части
f.size           # len(content)
f.save("/tmp/out.bin")
```

Разбор multipart выполняется в Rust. Файлы держатся в памяти в рамках
`max_body_size`.

```python
@app.route("/upload", methods=["POST"])
async def upload(req):
    files = await req.files()
    for upload in files.get("file", []):
        upload.save(f"/data/{upload.filename}")
    return {"saved": len(files.get("file", []))}
```

Неверный content type (`form()` на JSON, `files()` на не-multipart) бросает
`UnsupportedMediaType` → **415**.

## Лимит размера тела

```python
app = Ferrox(max_body_size=10 * 1024 * 1024)   # 10 MiB → 413 при превышении
```
