# Rust core — `ferrox._core`

The hot paths live in Rust (`ferrox-rs/`, built by maturin as the
`ferrox._core` extension module). Everything in it is reachable from Python but
most of it is used internally by the framework.

## Why Rust

| Concern | Implementation |
|---|---|
| Routing | `matchit` radix tree — O(1) lookup, no regex per request |
| Request parsing | method/path/query/headers parsed natively |
| JSON | `serde_json` with HTML-significant character escaping |
| gzip | Rust compression, honouring `Accept-Encoding` q-factors |
| CORS | preflight + origin resolution without Python |
| Multipart | native multipart/form-data parser |
| PostgreSQL | background tokio runtime, `query_json` (read-heavy paths) |

## Public surface

```python
from ferrox._core import FerroxApp, Request, Response, Router
from ferrox._core import db          # ferrox.db re-exports connect/query_json
```

### `FerroxApp`

```python
app = FerroxApp()
app.add_route(method, path, handler)
handler, params = app.resolve(method, path)     # None if no route
app.set_cors(origins, methods, headers, max_age)
app.cors_preflight_headers(origin)              # dict | None
app.cors_origin(origin)                          # allow-origin value | None
app.gzip_compress(data: bytes) -> bytes
```

### `Request` / `Response`

```python
r = Request(method, path, query_string)
r.set_headers(raw_bytes_headers)
r.headers / r.query / r.get_header(name)
r.set_body(body)
r.json()                          # ValueError on malformed input
r.parse_multipart(boundary)       # → (fields, files)

resp = Response.json(data, status)          # serde_json
resp = Response.text(text, status)
resp.body / resp.status / resp.content_type
```

### `Router`

A standalone conflict-free router over `method + path` patterns, usable without
the app for custom dispatch.

```python
router = Router()
router.add("GET", "/users/{id}", handler)
handler, params = router.lookup("GET", "/users/7")   # (handler, {"id": "7"}) | None
```

## The `.pyi`

Type stubs live in `ferrox/_core.pyi` and ship inside the wheel, so mypy and
IDEs see the native core's types even though the extension itself carries no
annotations.

## Building

```bash
PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 .venv/bin/maturin develop --release
```

The crate is built with the `abi3-py312` feature: one `cp312-abi3` wheel works
on every CPython 3.12+, at a measured cost of ~1% throughput versus a
version-specific build.
