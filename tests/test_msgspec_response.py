"""`_to_response`: сериализация `msgspec.Struct` быстрым C-энкодером msgspec.

Покрывает новые ветки `_to_response`: сам Struct, список Struct'ов и кортеж
``(payload, status)`` с ними. Пустой список не считается struct-payload и уходит
обычным Rust-путём.
"""

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
    v = Ferronit(debug=True)

    @v.route("/struct")
    def struct():
        return Item(name="widget", price=9.9)

    @v.route("/structs")
    def structs():
        return [Item(name="a", price=1.0), Item(name="b", price=2.0)]

    @v.route("/struct-404")
    def struct_404():
        return Item(name="missing", price=0.0), 404

    @v.route("/structs-207")
    def structs_207():
        return [Item(name="a", price=1.0)], 207

    @v.route("/empty")
    def empty():
        return []

    return v


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_struct_is_json_200(client) -> None:
    r = await client.get("/struct")
    assert r.status_code == 200
    assert r.json() == {"name": "widget", "price": 9.9}
    assert r.headers["content-type"].startswith("application/json")


async def test_list_of_structs_is_json_array(client) -> None:
    r = await client.get("/structs")
    assert r.status_code == 200
    assert r.json() == [{"name": "a", "price": 1.0}, {"name": "b", "price": 2.0}]


async def test_struct_with_status(client) -> None:
    r = await client.get("/struct-404")
    assert r.status_code == 404
    assert r.json() == {"name": "missing", "price": 0.0}


async def test_struct_list_with_status(client) -> None:
    r = await client.get("/structs-207")
    assert r.status_code == 207
    assert r.json() == [{"name": "a", "price": 1.0}]


async def test_empty_list_still_rust_json(client) -> None:
    r = await client.get("/empty")
    assert r.status_code == 200
    assert r.json() == []
