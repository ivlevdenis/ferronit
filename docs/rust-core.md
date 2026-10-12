# Rust-ядро — `ferronit._core`

## Зачем

Горячие пути живут в Rust (`ferronit-rs/`, собирается maturin-ом как расширение
`ferronit._core`). Всё в нём доступно из Python, но большая часть используется
фреймворком внутри — вам редко приходится трогать это напрямую.

| Задача | Реализация |
|---|---|
| Маршрутизация | radix-дерево `matchit` — O(1) поиск, без regex на запрос |
| Разбор запроса | method/path/query/headers парсятся нативно |
| JSON | `serde_json` с экранированием HTML-значимых символов |
| gzip | сжатие в Rust с учётом q-факторов `Accept-Encoding` |
| CORS | preflight и разрешение origin без Python |
| Multipart | нативный парсер multipart/form-data |

## Публичная поверхность

```python
from ferronit._core import FerronitApp, Request, Response, Router
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
app.handle_request(method, path, query_string, raw_headers, body)  # Response (бенчи)
```

### `Request` / `Response`

```python
r = Request(method, path, query_string)
r.set_headers(raw_bytes_headers)
r.headers / r.query / r.get_header(name)
r.set_body(body)
r.json()                          # ValueError при некорректном вводе
r.parse_multipart(boundary)       # → (fields, files)

resp = Response.json(data, status)          # serde_json
resp = Response.text(text, status)
resp.body / resp.status / resp.content_type
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
