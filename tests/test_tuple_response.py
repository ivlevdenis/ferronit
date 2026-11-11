"""The `return payload, status` pattern is part of the public handler contract.

Regression: before the `_to_response` fix the tuple was not understood, the response body
became the tuple's repr, and the status always stayed 200 (this broke /health and docs examples).
"""

import pytest
from httpx import ASGITransport, AsyncClient

from ferronit import Ferronit


@pytest.fixture
def client() -> AsyncClient:
    app = Ferronit(debug=True)

    @app.route("/dict-404")
    async def not_found():
        return {"error": "not found"}, 404

    @app.route("/text-201")
    async def created():
        return "created", 201

    @app.route("/list-207")
    async def multi_status():
        return [{"id": 1}], 207

    @app.route("/plain")
    async def plain():
        return {"ok": True}

    @app.route("/items")
    async def items():
        return [{"id": 1}, {"id": 2}]

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_dict_with_status_keeps_status_and_body(client):
    async with client as c:
        response = await c.get("/dict-404")

    assert response.status_code == 404
    assert response.json() == {"error": "not found"}


@pytest.mark.asyncio
async def test_text_with_status(client):
    async with client as c:
        response = await c.get("/text-201")

    assert response.status_code == 201
    assert response.text == "created"


@pytest.mark.asyncio
async def test_non_dict_payload_with_status(client):
    async with client as c:
        response = await c.get("/list-207")

    assert response.status_code == 207
    assert response.json() == [{"id": 1}]


@pytest.mark.asyncio
async def test_plain_dict_still_returns_200(client):
    async with client as c:
        response = await c.get("/plain")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_bare_list_is_serialised_as_json_array(client):
    """Regression: a list in the response became a Python repr instead of JSON."""
    async with client as c:
        response = await c.get("/items")

    assert response.status_code == 200
    assert response.json() == [{"id": 1}, {"id": 2}]
    assert response.headers["content-type"].startswith("application/json")
