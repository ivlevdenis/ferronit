# Ошибки и валидация входных данных

Любой отказ по вине клиента отдаётся одним JSON-конвертом — и для полей тела, и
для ошибок, которые к полю не относятся:

```json
{
  "code": "validation_error",
  "message": "Request validation failed: 2 errors",
  "errors": [
    {"code": "missing", "field": "name", "message": "Field 'name' is required"},
    {"code": "too_small", "field": "age", "message": "Expected `int` >= 1"}
  ]
}
```

- `code` верхнего уровня — класс отказа: `validation_error`, `invalid_json`,
  `not_found`, `unsupported_media_type`, `body_too_large`, `internal_error`;
- `errors` — **всегда** массив и никогда не пустой. Ошибка поля несёт `field`
  (точечный путь: `name`, `addr.zip`, `tags[0]`), ошибка «не поля» — нет;
- `message` — текст для человека и логов; клиент переключается по `code`,
  а не по тексту, поэтому тексты можно менять, коды — нет.

Конверт одинаков для 4xx и 5xx: один парсер на клиенте.

## Коды

| Код | Когда | `field` |
|---|---|---|
| `missing` | обязательное поле не передано | есть |
| `invalid_type` | значение не приводится к объявленному типу | есть (в 404 — тоже) |
| `too_small` | нарушена нижняя граница (`ge`/`gt`) | есть |
| `too_large` | нарушена верхняя граница (`le`/`lt`) | есть |
| `too_short` | короче `min_length` | есть |
| `too_long` | длиннее `max_length` | есть |
| `not_multiple_of` | не кратно `multiple_of` | есть |
| `pattern_mismatch` | не совпало с `pattern` | есть |
| `invalid_enum` | значение вне `Literal`/enum | есть |
| `invalid_value` | прочее нарушение значения | есть |
| `invalid_json` | тело не разбирается как JSON | нет |
| `not_found` | нет маршрута / битый path-параметр | нет / есть |
| `unsupported_media_type` | чужой `Content-Type` для формы/файлов | нет |
| `body_too_large` | тело больше `max_body_size` | нет |
| `internal_error` | необработанное исключение хендлера | нет |

`invalid_request` — общий код для прочих клиентских ошибок (например,
`multipart/form-data` без boundary).

## Откуда берутся ошибки

**Тело (`msgspec.Struct`).** `msgspec.json.decode` падает на первой ошибке, поэтому
при отказе фреймворк разбирает тело повторно и обходит объявленные поля — в ответ
попадают **все** ошибки сразу, включая вложенные структуры (`addr.zip`), элементы
списков (`tags[0]`) и обязательные поля внутри вложенных моделей. На валидном теле
работает прежний быстрый путь: один C-проход msgspec, без обхода.

**Query-модель.** Мягкая семантика сохраняется: невалидное значение откатывается к
`default` поля; если default нет — ошибки собираются и отдаются все сразу.

**Скаляры path/query/header.** Код `invalid_type` (или `missing`), `field` — имя
параметра. Битый path-параметр отдаёт **404**, чтобы не раскрывать существование
маршрута (ASVS 2.1.1), но тело ответа такое же структурированное.

**Не поля.** Битый JSON (`invalid_json`), корень не объект/массив (`invalid_type` без
`field`), чужой content-type (`415`), большое тело (`413`), отсутствующий маршрут
(`404`) — всё это записи в том же `errors`.

## Примеры ответов

Одно поле не того типа и одно отсутствует:

```json
{"code": "validation_error", "message": "Request validation failed: 2 errors",
 "errors": [
   {"code": "missing", "field": "name", "message": "Field 'name' is required"},
   {"code": "invalid_type", "field": "age", "message": "Expected `int`, got `str`"}
 ]}
```

Вложенная модель `addr: {city, zip}`:

```json
{"code": "validation_error", "message": "Request validation failed: 2 errors",
 "errors": [
   {"code": "missing", "field": "addr.city", "message": "Field 'addr.city' is required"},
   {"code": "too_small", "field": "addr.zip", "message": "Expected `int` >= 100000"}
 ]}
```

Битый JSON — ошибка не поля:

```json
{"code": "invalid_json", "message": "Malformed JSON body: Input data was truncated",
 "errors": [{"code": "invalid_json", "message": "Malformed JSON body: Input data was truncated"}]}
```

Битый path-параметр (404):

```json
{"code": "not_found", "message": "Invalid path parameter 'item_id': 'abc'",
 "errors": [{"code": "invalid_type", "field": "item_id",
             "message": "Invalid path parameter 'item_id': 'abc'"}]}
```

## Своя ошибка из хендлера

Бизнес-правило — тот же конверт: поднимите `ValidationError` со своими записями.
Код выбираете сами, поле можно не указывать.

```python
from ferronit import FieldError, ValidationError

@app.route("/signup", methods=["POST"])
async def signup(payload: Signup):
    if await users.by_email(payload.email):
        raise ValidationError([FieldError("email_taken", "Email already registered", "email")])
    ...
```

```json
{"code": "validation_error", "message": "Request validation failed: 1 error",
 "errors": [{"code": "email_taken", "field": "email", "message": "Email already registered"}]}
```

## Статусы

| Исключение | HTTP | `code` |
|---|---|---|
| `ValidationError` (в т.ч. из хендлера) | 400 | `validation_error` |
| `RequestError` | 400 | `invalid_request` / свой |
| `NotFoundError` | 404 | `not_found` |
| `PathParamError` | 404 | `not_found` |
| `UnsupportedMediaType` | 415 | `unsupported_media_type` |
| `BodyTooLarge` | 413 | `body_too_large` |
| любое другое | 500 | `internal_error` (текст исключения не отдаётся) |

При `Ferronit(debug=True)` необработанные исключения пробрасываются наружу, как и
раньше — удобно при разработке.
