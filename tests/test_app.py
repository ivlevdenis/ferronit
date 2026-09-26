"""Smoke test — ASGI compliance and basic routing."""

import pytest
from httpx import ASGITransport, AsyncClient

from velox import Velox


@pytest.fixture
def app() -> Velox:
    v = Velox(debug=True)

    @v.route("/")
    def home(req):
        return {"ok": True}

    @v.route("/hello")
    def hello(req):
        name = req.query.get("name", ["world"])[0]
        return f"Hello, {name}!"

    @v.route("/echo", methods=["POST"])
    def echo(req):
        return {"method": req.method, "path": req.path}

    return v


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_json_response(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


@pytest.mark.asyncio
async def test_text_response(client):
    r = await client.get("/hello?name=Velox")
    assert r.status_code == 200
    assert r.text == "Hello, Velox!"


@pytest.mark.asyncio
async def test_post_json(client):
    r = await client.post("/echo")
    assert r.json() == {"method": "POST", "path": "/echo"}


@pytest.mark.asyncio
async def test_404(client):
    r = await client.get("/nope")
    assert r.status_code == 404
