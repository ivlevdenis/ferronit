"""Injection tests — auto-resolve path/query params from handler signature."""

import pytest
from httpx import ASGITransport, AsyncClient

from velox import Velox


@pytest.fixture
def app():
    v = Velox(debug=True)

    @v.route("/users/{user_id}")
    def get_user(user_id: int, format: str = "json"):
        return {"id": user_id, "format": format}

    @v.route("/search")
    def search(q: str = "", limit: int = 10):
        return {"q": q, "limit": limit}

    @v.route("/echo")
    def echo(name: str = "World"):
        return {"hello": name}

    return v


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_path_param_injection(client):
    r = await client.get("/users/42")
    assert r.json() == {"id": 42, "format": "json"}


@pytest.mark.asyncio
async def test_query_param_injection(client):
    r = await client.get("/search?q=velox&limit=25")
    assert r.json() == {"q": "velox", "limit": 25}


@pytest.mark.asyncio
async def test_default_query_param(client):
    r = await client.get("/search")
    assert r.json() == {"q": "", "limit": 10}


@pytest.mark.asyncio
async def test_echo_default(client):
    r = await client.get("/echo?name=Velox")
    assert r.json() == {"hello": "Velox"}


@pytest.mark.asyncio
async def test_invalid_path_param_returns_404(client):
    r = await client.get("/users/abc")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_invalid_query_param_returns_400(client):
    r = await client.get("/search?limit=abc")
    assert r.status_code == 400
