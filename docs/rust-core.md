# Rust-ядро — `ferronit._core`

## Зачем

Горячие пути живут в Rust (`ferronit-rs/`, собирается maturin-ом как расширение
`ferronit._core`). Всё в нём доступно из Python, но большая часть используется
фреймворком внутри — вам редко приходится трогать это напрямую.

| Задача | Реализация |
|---|---|
| Маршрутизация | radix-дерево `matchit` — O(1) поиск, без regex на запрос |
| Разбор запроса | method/path/query/headers/cookies парсятся нативно |
| Заголовки ответа | упорядоченные field-lines, валидация RFC 9110 |
| gzip | сжатие в Rust с учётом q-факторов `Accept-Encoding` |
| CORS | preflight и разрешение origin без Python |
| Multipart | нативный парсер multipart/form-data |

## Публичная поверхность

```python
from ferronit._core import (
    FerronitApp,
    Headers,
    Request,
    Router,
    build_set_cookie,
    first_header,
    parse_headers,
)
```

### `FerronitApp`

```python
app = FerronitApp()
app.add_route(method, path, handler)
handler, params = app.resolve(method, path)     # None, если роута нет
app.set_cors(origins, methods, headers, max_age)
app.has_cors()                                  # bool — включён ли CORS
app.cors_allows(origin)                         # bool — разрешён ли origin
app.cors_is_wildcard()                          # bool — разрешён ли "*"
app.cors_preflight_headers(origin)              # dict | None
app.cors_origin(origin)                         # значение allow-origin | None
app.gzip_compress(data: bytes) -> bytes
```

### `Request`

```python
r = Request(method, path, query_string)
r.set_headers(raw_pairs)          # список пар bytes (ASGI) или str (RSGI)
r.headers                         # dict: повторы склеены по RFC 9110
r.get_header(name)                # первое значение | None
r.get_all(name)                   # все значения в порядке провода
r.cookies                         # разобранный Cookie (повторы через "; ")
r.query                           # dict[str, list[str]]
r.set_body(body)
r.parse_multipart(boundary)       # → (fields, files)
```

### `Headers`

```python
h = Headers()                     # или mapping / список пар / другой Headers
h.add(name, value)                # дописать поле (дубликаты сохраняются)
h["x-tag"] = value                # заменить все строки поля одной
del h["x-tag"]
h["x-tag"] / h.get(name, default) / h.get_all(name) / h.setdefault(name, value) / h.pop(name)
list(h)                           # пары (name, value) в порядке добавления
h.keys() / h.values() / h.items()
h.raw()                           # ASGI: [(bytes, bytes)]
h.str_pairs()                     # RSGI: [(str, str)]
```

### Функции

```python
build_set_cookie(name, value="", *, path=None, domain=None, max_age=None,
                 expires=None, http_only=False, secure=False, same_site=None) -> str
parse_headers(raw_pairs) -> dict[str, str]     # те же правила склейки, что у Request.headers
first_header(raw, name) -> str | None          # список пар или объект с items()
```

### `Router`

Автономный маршрутизатор без конфликтов по паттернам `method + path`,
используемый без приложения для кастомного диспетчинга.

```python
router = Router()
router.add("GET", "/users/{id}", handler)
handler, params = router.lookup("GET", "/users/7")   # (handler, {"id": "7"}) | None
```

## `.pyi`

Стабы типов лежат в `ferronit/_core.pyi` и попадают в wheel, поэтому mypy и IDE
видят типы нативного ядра, хотя само расширение аннотаций не несёт.

## Сборка

```bash
PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 poetry run maturin develop --release
```

Крейт собран с фичей `abi3-py312`: один wheel `cp312-abi3` работает на любом
CPython 3.12+ при измеренной цене ~1% throughput против сборки под конкретную
версию.
