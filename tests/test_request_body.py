"""`Request.model()`: декод JSON в msgspec.Struct (быстрый путь) и fallback."""

from __future__ import annotations

import msgspec
import pytest
from httpx import ASGITransport, AsyncClient

from ferronit import Ferronit


class Item(msgspec.Struct):
    name: str
    price: float


@pytest.fixture
def app():
    v = Ferronit()

    @v.route("/item", methods=["POST"])
    async def create(req):
        item = await req.model(Item)
        return {"name": item.name, "price": item.price}

    return v


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_model_msgspec_struct(client) -> None:
    response = await client.post("/item", json={"name": "widget", "price": 9.9})
    assert response.status_code == 200
    assert response.json() == {"name": "widget", "price": 9.9}


async def test_model_msgspec_invalid_type_is_400(client) -> None:
    response = await client.post("/item", json={"name": "widget", "price": "oops"})
    assert response.status_code == 400


async def test_model_msgspec_malformed_json_is_400(client) -> None:
    response = await client.post(
        "/item", content=b"not-json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400


async def test_model_msgspec_array_body_is_400(client) -> None:
    """Struct требует объект — массив/скаляр отклоняются с 400."""
    response = await client.post("/item", json=[1, 2, 3])
    assert response.status_code == 400
