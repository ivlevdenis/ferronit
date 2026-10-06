"""Паттерн `return payload, status` — часть публичного контракта хендлеров.

Регрессия: до исправления `_to_response` кортеж не понимался, тело ответа превращалось
в repr кортежа, а статус всегда оставался 200 (это и ломало /health, и примеры в docs).
"""

import pytest
from httpx import ASGITransport, AsyncClient

from ferrox import Ferrox


@pytest.fixture
def client() -> AsyncClient:
    app = Ferrox(debug=True)

    @app.route("/dict-404")
    def not_found():
        return {"error": "not found"}, 404

    @app.route("/text-201")
    def created():
        return "created", 201

    @app.route("/list-207")
    def multi_status():
        return [{"id": 1}], 207

    @app.route("/plain")
    def plain():
        return {"ok": True}

    @app.route("/items")
    def items():
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
    """Регрессия: список в ответе превращался в Python-repr вместо JSON."""
    async with client as c:
        response = await c.get("/items")

    assert response.status_code == 200
    assert response.json() == [{"id": 1}, {"id": 2}]
    assert response.headers["content-type"].startswith("application/json")
