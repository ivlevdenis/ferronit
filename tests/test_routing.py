"""Routing tests — static routes, parameterised routes, 404."""

import pytest
from httpx import ASGITransport, AsyncClient

from velox import Velox


@pytest.fixture
def app():
    v = Velox(debug=True)

    @v.route("/users/{user_id}")
    def get_user(req):
        return {"user_id": req.params["user_id"]}

    @v.route("/posts/{post_id}/comments/{comment_id}")
    def get_comment(req):
        return {"post_id": req.params["post_id"], "comment_id": req.params["comment_id"]}

    @v.route("/")
    def home(req):
        return {"root": True}

    return v


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_root(client):
    r = await client.get("/")
    assert r.json() == {"root": True}


@pytest.mark.asyncio
async def test_parameterised(client):
    r = await client.get("/users/42")
    assert r.json() == {"user_id": "42"}


@pytest.mark.asyncio
async def test_nested_parameterised(client):
    r = await client.get("/posts/10/comments/99")
    assert r.json() == {"post_id": "10", "comment_id": "99"}


@pytest.mark.asyncio
async def test_404_parameterised(client):
    r = await client.get("/users")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_404_unmatched(client):
    r = await client.get("/nope")
    assert r.status_code == 404
