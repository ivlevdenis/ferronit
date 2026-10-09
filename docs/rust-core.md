# Rust-ядро — `ferrox._core`

## Зачем

Горячие пути живут в Rust (`ferrox-rs/`, собирается maturin-ом как расширение
`ferrox._core`). Всё в нём доступно из Python, но большая часть используется
фреймворком внутри — вам редко приходится трогать это напрямую.

| Задача | Реализация |
|---|---|
| Маршрутизация | radix-дерево `matchit` — O(1) поиск, без regex на запрос |
| Разбор запроса | method/path/query/headers парсятся нативно |
| JSON | `serde_json` с экранированием HTML-значимых символов |
| gzip | сжатие в Rust с учётом q-факторов `Accept-Encoding` |
| CORS | preflight и разрешение origin без Python |
| Multipart | нативный парсер multipart/form-data |
| PostgreSQL | фоновый tokio-рантайм, `query_json` (read-heavy пути) |

## Публичная поверхность

```python
from ferrox._core import FerroxApp, Request, Response, Router
from ferrox._core import db          # ferrox.db реэкспортирует connect/query_json
```

### `FerroxApp`

```python
app = FerroxApp()
app.add_route(method, path, handler)
handler, params = app.resolve(method, path)     # None, если роута нет
app.set_cors(origins, methods, headers, max_age)
app.cors_preflight_headers(origin)              # dict | None
app.cors_origin(origin)                          # значение allow-origin | None
app.gzip_compress(data: bytes) -> bytes
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

Стабы типов лежат в `ferrox/_core.pyi` и попадают в wheel, поэтому mypy и IDE
видят типы нативного ядра, хотя само расширение аннотаций не несёт.

## Сборка

```bash
PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 .venv/bin/maturin develop --release
```

Крейт собран с фичей `abi3-py312`: один wheel `cp312-abi3` работает на любом
CPython 3.12+ при измеренной цене ~1% throughput против сборки под конкретную
версию.
