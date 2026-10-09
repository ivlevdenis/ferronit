# Routing & handlers

## Registering routes

```python
from ferrox import Ferrox

app = Ferrox()

@app.route("/ping")
async def ping():
    return {"ok": True}

@app.route("/users/{user_id}")
async def get_user(user_id: int):
    return {"id": user_id}

@app.route("/users", methods=["POST"])
async def create_user():
    return {"created": True}
```

`@app.route(path, methods=None, **meta)`:

- `path` — route pattern; path parameters use `{name}`.
- `methods` — list of HTTP methods, defaults to `["GET"]`.
- extra keywords (`summary=`, `tags=`, ...) are forwarded to the OpenAPI builder.

Routing itself happens in **Rust** (`matchit` radix tree) — O(1) lookup, no
per-request Python overhead, and no degradation with thousands of routes.

## Handlers

Handlers may be `async def` or plain `def`. A handler with no type hints that
takes a single `Request` argument runs through a zero-wrapping fast path.

## Return values

Any of these work; the framework normalises them:

```python
@app.route("/a")
async def a():
    return {"ok": True}                    # dict → JSON, 200

@app.route("/b")
async def b():
    return ["x", "y"]                      # list → JSON, 200

@app.route("/c")
async def c():
    return {"error": "not found"}, 404     # (payload, status) tuple

@app.route("/d")
async def d():
    return "hello"                         # str → text/plain

@app.route("/e")
async def e():
    return Response(b"raw", content_type="application/octet-stream")

@app.route("/f")
async def f():
    return SomeModel(name="a")             # msgspec/dataclass/pydantic → JSON

@app.route("/g")
async def g():
    async def stream():
        for i in range(5):
            yield f"data: {i}\n\n"
    return stream()                        # async iterator → streaming
```

Anything else is sent as `text/plain` via `str(result)`.

## Responses

```python
from ferrox import Response, TextResponse, JSONResponse, StreamingResponse
```

| Class | Purpose |
|---|---|
| `Response(body, status=200, content_type="text/plain", headers=None)` | raw body |
| `TextResponse(text, status=200)` | UTF-8 text |
| `JSONResponse(data, status=200, headers=None)` | JSON serialisation |
| `StreamingResponse(iterator, status=200, content_type="text/event-stream")` | chunked / SSE |

Headers values are scrubbed against CRLF/LF injection before being sent.

## Path parameters

Path parameters are declared in the pattern and injected by name (see
[injection](injection.md)). A parameter that fails coercion — e.g. `/users/abc`
for `user_id: int` — returns **404**, deliberately hiding whether the route
exists (ASVS 2.1.1).

## Error handling

| Exception | HTTP |
|---|---|
| `PathParamError` | 404 |
| `RequestError` | 400 |
| `UnsupportedMediaType` | 415 |
| `BodyTooLarge` | 413 |
| any other | 500 (re-raised when `debug=True`) |

Set `debug=True` to see exceptions during development:

```python
app = Ferrox(debug=True)
```
