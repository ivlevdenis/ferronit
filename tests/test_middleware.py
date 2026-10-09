"""Middleware tests."""

import pytest
from httpx import ASGITransport, AsyncClient

from ferronit import Ferronit


@pytest.fixture
def app():
    v = Ferronit(debug=True)

    async def logger(req, next_handler):
        req._scope["x-mw"] = "logged"
        result = next_handler(req)
        if hasattr(result, "__await__"):
            result = await result
        return result

    v.use(logger)

    @v.route("/")
    def home(req):
        return {"mw": req._scope.get("x-mw", "none")}

    @v.route("/hello")
    async def hello(req):
        return f"Hello from {req._scope['x-mw']}"

    return v


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_middleware_sync(client):
    r = await client.get("/")
    assert r.json() == {"mw": "logged"}


@pytest.mark.asyncio
async def test_middleware_async(client):
    r = await client.get("/hello")
    assert r.text == "Hello from logged"
