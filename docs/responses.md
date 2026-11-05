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

`headers` принимает как mapping, так и список пар — второй нужен, когда поле
повторяется.

## Заголовки

Заголовки ответа — упорядоченная последовательность field-lines (RFC 9110 §5.3),
а не словарь; контейнер `Headers` реализован в Rust-ядре (`ferronit._core`): порядок значим, повторы сохраняются, поэтому несколько
`Set-Cookie` доходят до клиента как отдельные строки. Имена приводятся к нижнему
регистру (требование ASGI), значения обязаны быть latin-1. Итерация отдаёт пары:

```python
from ferronit import JSONResponse

resp = JSONResponse({"ok": True}, headers=[("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")])
list(resp.headers)      # [('content-type', 'application/json; charset=utf-8'),
                        #  ('set-cookie', 'a=1'), ('set-cookie', 'b=2')]

resp.headers.add("Set-Cookie", "c=3")   # добавить ещё одну строку
resp.headers["X-Tag"] = "v"             # заменить все строки x-tag одной
resp.headers.get_all("Set-Cookie")      # ['a=1', 'b=2', 'c=3']
resp.headers["Set-Cookie"]              # 'a=1' — первое значение
```

Чтение регистронезависимо; повторённые поля, кроме `set-cookie`, склеиваются
через `", "` (RFC 9110 §5.2) — `set-cookie` склеивать запятой запрещено
(RFC 6265), поэтому для него возвращается первая строка. Невалидное имя
(не токен) и не-latin-1 значение дают `ValueError` при записи.

Значения нормализуются по RFC 9110 §5.5: CR, LF и NUL заменяются на SP (спека
разрешает получателю reject или SP-replace; SP не теряет данные, например
`"a\r\nb"` → `"a  b"`), любой другой CTL — `ValueError`, ведущие и хвостовые
SP/HTAB отбрасываются как OWS. Внутренние HTAB, SP, VCHAR и obs-text
(0x80–0xFF) сохраняются. WebSocket-хендшейк идёт через тот же контракт:
`conn.accept(headers=...)` принимает mapping или пары и нормализует их одинаково.

## Куки

`Set-Cookie` — повторяемое поле, поэтому куки ставятся через `add`, а не
присваиванием. Готовый путь — `set_cookie`/`delete_cookie`: сериализация и
валидация по RFC 6265 §4.1.1 выполняются в Rust-ядре, наружу уходят отдельные
field-lines.

```python
resp = JSONResponse({"ok": True})
resp.set_cookie("sid", "abc123", path="/app", http_only=True, secure=True)
resp.set_cookie("theme", "dark")          # вторая строка, первая не затирается
resp.delete_cookie("legacy")              # Max-Age=0 + истёкший Expires
```

`set_cookie(name, value="", *, path="/", domain=None, max_age=None, expires=None,
http_only=False, secure=False, same_site="Lax")`. Имя — токен; значение —
`cookie-octet` (без CTL, пробела, `"`, `,`, `;`, `\`); `path`/`domain`/`expires`
не могут содержать CTL или `;`. Нарушение — `ValueError`, ничего не уходит в
ответ. `expires` принимает готовую HTTP-дату строкой.

## Модели в ответе

Возврат модели из хендлера автоматически сериализует её в JSON. Распознаются
модели с `model_dump` (Pydantic), `__dataclass_fields__`, `__columns__`
(rawmodel) или `__struct_fields__` (msgspec). msgspec-модели (и списки из них)
сериализуются C-энкодером msgspec, rawmodel и dataclass — stdlib `json`
(fallback), Pydantic — собственным dump.

```python
@app.route("/me")
async def me():
    return User(id=1, name="Ada")     # msgspec.Struct → JSON (msgspec C-энкодер)
```

## Какие модели поддерживаются

Для тел запросов и ответов — три вида моделей:

1. **`msgspec.Struct`** — быстрый путь: декод из байтов тела и сериализация ответа
   C-энкодером msgspec. Рекомендуемый вариант для горячих путей.
2. **Pydantic v2** — через зарегистрированный кодек (валидация богаче).
3. **`@dataclass`** — fallback-кодек без внешних зависимостей.

## Кастомные кодеки

`ferronit.contrib.pydantic` предоставляет подключаемый реестр кодеков:

```python
from ferronit.contrib.pydantic import register_codec, PydanticCodec

register_codec(MyType, MyCodec)   # decode_json / encode_json теперь знают MyType
```

msgspec входит в ядро (обязательная зависимость); Pydantic остаётся
опциональным и импортируется только при использовании.
