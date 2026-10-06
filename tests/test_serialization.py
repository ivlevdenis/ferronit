"""Serialization tests — Pydantic codec integration."""

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from ferrox import Ferrox, JSONResponse
from ferrox.contrib.pydantic.pydantic_codec import install as install_pydantic

install_pydantic()


class Item(BaseModel):
    name: str
    price: float


@pytest.fixture
def app():
    v = Ferrox(debug=True)

    @v.route("/items", methods=["POST"])
    async def create(req):
        item = await req.model(Item)
        return JSONResponse.from_model(item, status=201)

    return v


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_pydantic_model_roundtrip(client):
    r = await client.post("/items", json={"name": "widget", "price": 9.99})
    assert r.status_code == 201
    assert r.json() == {"name": "widget", "price": 9.99}
