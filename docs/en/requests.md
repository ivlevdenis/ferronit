# Requests

The `Request` object wraps the ASGI scope with **Rust-native parsing**. Headers
and query string are parsed lazily (on first access) and cached for the request's
lifetime.

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

## Attributes & methods

| Member | Type | Notes |
|---|---|---|
| `req.params` | `dict[str, str]` | path parameters captured by the route |
| `req.method` | `str` | e.g. `"GET"` |
| `req.path` | `str` | path without query string |
| `req.headers` | `dict[str, str]` | lower-cased keys, lazy |
| `req.get_header(name)` | `str \| None` | case-insensitive single lookup |
| `req.query` | `dict[str, list[str]]` | **every value is a list** (repeated keys preserved) |
| `req.cookies` | `dict[str, str]` | parsed `Cookie` header |
| `req.get_cookie(name, default=None)` | `str \| None` | single cookie |

Query values are always lists — `?a=1&a=2` becomes `{"a": ["1", "2"]}`. For a
typed scalar use dependency injection (see [injection](injection.md)).

## Reading the body

```python
body: bytes = await req.body()          # full body; raises BodyTooLarge past the limit
data = await req.json()                 # parse JSON; RequestError (→400) on malformed
user = await req.model(UserModel)       # decode into msgspec/dataclass/pydantic
```

- `body()` streams ASGI chunks. When the app was created with `max_body_size`,
  reading past it raises `BodyTooLarge` → **413**.
- `model()` has a fast path for `msgspec.Struct` — decoded from bytes in one C
  pass; Pydantic and dataclasses go through the registered codec.

## Forms & files

```python
fields = await req.form()    # urlencoded → {name: [values]}; multipart → text fields only
files = await req.files()    # multipart → {name: [UploadedFile]}
```

`UploadedFile`:

```python
f.filename       # original file name
f.content        # bytes
f.content_type   # part content type
f.size           # len(content)
f.save("/tmp/out.bin")
```

Multipart parsing runs in Rust. Files stay in memory within `max_body_size`.

```python
@app.route("/upload", methods=["POST"])
async def upload(req):
    files = await req.files()
    for upload in files.get("file", []):
        upload.save(f"/data/{upload.filename}")
    return {"saved": len(files.get("file", []))}
```

Wrong content type (`form()` on JSON, `files()` on non-multipart) raises
`UnsupportedMediaType` → **415**.

## Body size limit

```python
app = Ferrox(max_body_size=10 * 1024 * 1024)   # 10 MiB → 413 beyond
```
