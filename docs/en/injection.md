# Dependency injection

Handlers declare parameters with type hints; the framework resolves them
**at registration time** — every request just calls a pre-built coercer, so the
cost is ~0.3 µs per parameter.

```python
@app.route("/search")
async def search(q: str, page: int = 1, per_page: int = 20):
    ...
```

Resolution order for a scalar parameter:

1. **path parameter** with the same name (wins over query);
2. **query string** value;
3. the declared **default**.

## Typed parameters

| Hint | Behaviour |
|---|---|
| `str` (or unknown) | passed through as-is |
| `int` / `float` | coerced; invalid → 400 (query/header) or 404 (path) |
| `bool` | accepts `true/false/1/0/yes/no/on/off` (case-insensitive) |
| `list[T]` | repeated values, each coerced with `T` |
| `int \| str` (union) | members tried in declaration order |
| `req` / `Request` | the `Request` object itself |

```python
@app.route("/report")
async def report(year: int, tags: list[str], draft: bool = False):
    ...
# /report?year=2025&tags=a&tags=b&draft=yes
```

## Headers

Mark a parameter with `Header` via `typing.Annotated`:

```python
from typing import Annotated
from ferrox import Header

@app.route("/whoami")
async def whoami(user_agent: Annotated[str, Header("User-Agent")]):
    return {"ua": user_agent}

@app.route("/api")
async def api(x_api_key: Annotated[str, Header()] = ""):
    # Header() → header name = parameter name ("x-api-key")
    ...
```

## Body models

A parameter whose hint is a body model (`msgspec.Struct`, dataclass or Pydantic)
is decoded from the JSON body via `await req.model(...)`. **Body parameters
require an `async` handler.**

```python
class NewUser(msgspec.Struct):
    name: str
    email: str

@app.route("/users", methods=["POST"])
async def create_user(user: NewUser):
    return {"id": 1, "name": user.name}
```

## Missing vs invalid

- **Missing** parameter without a default → `RequestError` → **400**
  (path → **404**).
- **Invalid** value (e.g. `page=abc` for `int`) → `RequestError` → **400**
  (path → **404**).
- `""` empty string falls back to the default when one is declared.
