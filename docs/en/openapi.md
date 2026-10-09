# OpenAPI

Every registered route feeds the OpenAPI 3.0 schema builder automatically.
`SchemaBuilder` infers types from signatures and models.

```python
app = Ferrox()

@app.route("/users/{user_id}", summary="Fetch a user", tags=["users"])
async def get_user(user_id: int):
    return {"id": user_id}
```

The `app.openapi` builder is available for customisation:

```python
schema = app.openapi   # OpenAPI instance (SchemaBuilder)
```

## Model schemas

`SchemaBuilder.from_model`:

- **Pydantic** → `model_json_schema()` (internal keys stripped);
- **dataclass** → object with fields (typed as strings);
- **msgspec / rawmodel** → object whose `__columns__` become string properties.

```python
from ferrox.openapi import SchemaBuilder

fragment = SchemaBuilder.from_model(NewUser)
```

## Serving the document

`OpenAPI` exposes `build()` (dict) and `json()` (string). Wire it to a route:

```python
@app.route("/openapi.json")
async def openapi_json():
    return Response(app.openapi.json(), content_type="application/json")
```

(Route metadata — `summary`, `tags`, etc. — is passed through the `@app.route`
decorator's extra keyword arguments. The handler's return annotation, when it is
a model, is stored under `components/schemas` and referenced from the `200`
response.)
