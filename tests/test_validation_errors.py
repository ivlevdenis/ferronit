"""Structured request errors: a code per error, ``errors`` holds fields and non-fields.

Контракт ответа (одинаковый для 4xx и 5xx)::

    {"code": "validation_error", "message": "...",
     "errors": [{"code": "missing", "field": "name", "message": "..."}]}
"""

import typing
from typing import Annotated

import msgspec
import pytest
from httpx import ASGITransport, AsyncClient

from ferronit import Body, Ferronit, FieldError, Header, Query, ValidationError


class Address(msgspec.Struct):
    city: str
    zip: Annotated[int, msgspec.Meta(ge=100000, le=999999)]


class Item(msgspec.Struct):
    name: str
    age: Annotated[int, msgspec.Meta(ge=1, le=150)] = 18
    kind: typing.Literal["a", "b"] = "a"
    tags: list[str] = []
    addr: Address | None = None


class Search(msgspec.Struct):
    q: str
    page: Annotated[int, msgspec.Meta(ge=1)] = 1
    per_page: Annotated[int, msgspec.Meta(ge=1, le=100)] = 20


class RequiredQuery(msgspec.Struct):
    page: Annotated[int, msgspec.Meta(ge=1)]
    size: Annotated[int, msgspec.Meta(le=10)]


@pytest.fixture
def app():
    v = Ferronit()

    @v.route("/items/{item_id}")
    def get_item(item_id: int):
        return {"id": item_id}

    @v.route("/items", methods=["POST"])
    async def create_item(payload: Item):
        return {"name": payload.name}

    @v.route("/search")
    def search(query: Annotated[Search, Query()]):
        return {"q": query.q, "page": query.page}

    @v.route("/required")
    def required(query: Annotated[RequiredQuery, Query()]):
        return {"page": query.page}

    @v.route("/scalar")
    def scalar(limit: int = 10, x_user_id: Annotated[int, Header("x-user-id")] = 0):
        return {"limit": limit, "uid": x_user_id}

    @v.route("/signup", methods=["POST"])
    async def signup(payload: Annotated[Item, Body()]):
        # бизнес-ошибка: не «поле не того типа», а правило предметной области
        raise ValidationError([FieldError("email_taken", "Email already registered", "name")])

    @v.route("/boom")
    def boom():
        raise RuntimeError("secret traceback")

    return v


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _codes(body: dict) -> list[str]:
    return [e["code"] for e in body["errors"]]


async def test_missing_and_wrong_type_are_separate_errors(client) -> None:
    """Оба поля попадают в один ответ, у каждого свой код и путь."""
    r = await client.post("/items", content=b'{"age": "x"}')
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "validation_error"
    assert body["message"] == "Request validation failed: 2 errors"
    assert body["errors"] == [
        {"code": "missing", "message": "Field 'name' is required", "field": "name"},
        {"code": "invalid_type", "message": "Expected `int`, got `str`", "field": "age"},
    ]


async def test_all_field_errors_collected_at_once(client) -> None:
    """msgspec падает на первой ошибке — фреймворк отдаёт все четыре."""
    r = await client.post(
        "/items", content=b'{"name": 5, "age": 0, "kind": "c", "tags": [1]}'
    )
    assert r.status_code == 400
    assert _codes(r.json()) == ["invalid_type", "too_small", "invalid_enum", "invalid_type"]
    assert [e["field"] for e in r.json()["errors"]] == ["name", "age", "kind", "tags[0]"]


async def test_constraint_bounds_have_codes(client) -> None:
    """Нарушение границ — свой код, а не общий invalid_value."""
    too_big = await client.post("/items", content=b'{"name": "x", "age": 999}')
    assert too_big.json()["errors"] == [
        {"code": "too_large", "message": "Expected `int` <= 150", "field": "age"}
    ]


async def test_nested_struct_paths_are_dotted(client) -> None:
    """Вложенная модель: путь addr.zip и обязательные поля внутри неё."""
    r = await client.post("/items", content=b'{"name": "x", "addr": {"zip": 5}}')
    assert r.status_code == 400
    assert _codes(r.json()) == ["missing", "too_small"]
    assert [e["field"] for e in r.json()["errors"]] == ["addr.city", "addr.zip"]


async def test_nested_struct_wrong_type(client) -> None:
    """Вместо объекта пришла строка — ошибка на самом поле addr."""
    r = await client.post("/items", content='{"name": "x", "addr": "Москва"}'.encode())
    assert r.json()["errors"] == [
        {"code": "invalid_type", "message": "Expected `object`, got `string`", "field": "addr"}
    ]


async def test_malformed_json_is_non_field_error(client) -> None:
    """Битый JSON — ошибка не поля: field отсутствует, код invalid_json."""
    r = await client.post("/items", content=b'{"name":')
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "invalid_json"
    assert len(body["errors"]) == 1
    assert body["errors"][0]["code"] == "invalid_json"
    assert "field" not in body["errors"][0]
    assert body["errors"][0]["message"].startswith("Malformed JSON body:")


async def test_body_root_not_object(client) -> None:
    """Корень — массив: ошибка без поля, код invalid_type."""
    r = await client.post("/items", content=b"[1, 2]")
    assert r.status_code == 400
    assert r.json()["errors"] == [
        {"code": "invalid_type", "message": "Expected `object`, got `array`"}
    ]


async def test_query_model_missing_fields_collected(client) -> None:
    """Обязательные поля query-модели — все сразу, каждое со своим путём."""
    r = await client.get("/required")
    assert r.status_code == 400
    assert _codes(r.json()) == ["missing", "missing"]
    assert [e["field"] for e in r.json()["errors"]] == ["page", "size"]


async def test_query_model_constraint_error(client) -> None:
    """Обязательное поле с нарушенным констрейнтом — код границы, не типа."""
    r = await client.get("/required?page=0&size=11")
    assert r.status_code == 400
    assert _codes(r.json()) == ["too_small", "too_large"]


async def test_query_model_invalid_value_with_default_falls_back(client) -> None:
    """Невалидное значение с default — тихий откат, ответ 200 (решение по query)."""
    r = await client.get("/search?q=x&page=0")
    assert r.status_code == 200
    assert r.json() == {"q": "x", "page": 1}


async def test_scalar_query_param_error(client) -> None:
    """Скалярный параметр: код invalid_type и имя поля."""
    r = await client.get("/scalar?limit=abc")
    assert r.status_code == 400
    assert r.json()["errors"] == [
        {
            "code": "invalid_type",
            "message": "Invalid query parameter 'limit': 'abc'",
            "field": "limit",
        }
    ]


async def test_header_param_error(client) -> None:
    """Заголовок валидируется так же, как query."""
    r = await client.get("/scalar", headers={"x-user-id": "nope"})
    assert r.status_code == 400
    assert r.json()["errors"][0]["field"] == "x_user_id"
    assert r.json()["errors"][0]["code"] == "invalid_type"


async def test_path_param_error_is_404_with_code(client) -> None:
    """Битый path-параметр: 404 (ASVS 2.1.1), но тело структурированное."""
    r = await client.get("/items/abc")
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == "not_found"
    assert body["errors"] == [
        {
            "code": "invalid_type",
            "message": "Invalid path parameter 'item_id': 'abc'",
            "field": "item_id",
        }
    ]


async def test_unknown_route_is_404_with_code(client) -> None:
    """Нет маршрута — та же схема ответа, код not_found."""
    r = await client.get("/nope")
    assert r.status_code == 404
    assert r.json() == {
        "code": "not_found",
        "message": "Route not found",
        "errors": [{"code": "not_found", "message": "Route not found"}],
    }


async def test_business_validation_error_from_handler(client) -> None:
    """Хендлер может поднять своё правило — код и поле доходят до клиента."""
    r = await client.post("/signup", content=b'{"name": "ada@example.com"}')
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "validation_error"
    assert body["errors"] == [
        {
            "code": "email_taken",
            "message": "Email already registered",
            "field": "name",
        }
    ]


async def test_unsupported_media_type_is_415_with_code() -> None:
    """415 отдаёт тот же конверт, а не пустое тело."""
    v = Ferronit()

    @v.route("/form", methods=["POST"])
    async def form(req):
        return await req.form()

    async with AsyncClient(
        transport=ASGITransport(app=v), base_url="http://test"
    ) as client:
        r = await client.post("/form", content=b"x", headers={"content-type": "text/plain"})
    assert r.status_code == 415
    assert r.json()["code"] == "unsupported_media_type"
    assert "field" not in r.json()["errors"][0]


async def test_body_too_large_is_413_with_code() -> None:
    """413 — тот же конверт с кодом body_too_large."""
    v = Ferronit(max_body_size=4)

    @v.route("/upload", methods=["POST"])
    async def upload(req):
        return {"size": len(await req.body())}

    async with AsyncClient(
        transport=ASGITransport(app=v), base_url="http://test"
    ) as client:
        r = await client.post("/upload", content=b"x" * 64)
    assert r.status_code == 413
    assert r.json()["code"] == "body_too_large"


async def test_unhandled_error_is_500_without_traceback() -> None:
    """500 не светит текст исключения, но остаётся в общей схеме."""
    v = Ferronit()

    @v.route("/boom")
    def boom():
        raise RuntimeError("secret traceback")

    async with AsyncClient(
        transport=ASGITransport(app=v), base_url="http://test"
    ) as client:
        r = await client.get("/boom")
    assert r.status_code == 500
    body = r.json()
    assert body["code"] == "internal_error"
    assert "secret" not in r.text


async def test_errors_never_empty_and_codes_are_stable(client) -> None:
    """payload() всегда отдаёт непустой errors; коды — короткие строки."""
    r = await client.get("/required")
    body = r.json()
    assert body["errors"]
    for error in body["errors"]:
        assert error["code"].islower()
        assert error["message"]
