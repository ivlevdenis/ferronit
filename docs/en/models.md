# Models & serialisation

## Body models

Three model kinds are supported for request bodies:

1. **`msgspec.Struct`** — the fast path: decoded from body bytes in one C pass.
2. **Pydantic v2** — via the registered codec.
3. **`@dataclass`** — fallback codec.

```python
import msgspec

class NewUser(msgspec.Struct):
    name: str
    email: str
    age: int = 0
```

Use it as a handler parameter (auto-decoded) or explicitly:

```python
@app.route("/users", methods=["POST"])
async def create_user(req):
    user = await req.model(NewUser)   # RequestError → 400 on validation failure
    return {"name": user.name}
```

## Responses

Returning a model from a handler serialises it to JSON automatically. Models
with `model_dump` (Pydantic), `__dataclass_fields__`, `__columns__` (rawmodel)
or `__struct_fields__` (msgspec) are all recognised. msgspec and rawmodel
models are serialised by the Rust encoder (serde_json), Pydantic through its
own dump.

```python
@app.route("/me")
async def me():
    return User(id=1, name="Ada")     # msgspec.Struct → JSON in Rust
```

## Custom codecs

`ferrox.contrib.pydantic` provides a pluggable codec registry:

```python
from ferrox.contrib.pydantic import register_codec, PydanticCodec

register_codec(MyType, MyCodec)   # decode_json / encode_json now know MyType
```

The core stays dependency-free: Pydantic is only imported when used.
