"""Injection tests — auto-resolve path/query params from handler signature."""

import pytest
from httpx import ASGITransport, AsyncClient

from ferrox import Ferrox


@pytest.fixture
def app():
    v = Ferrox(debug=True)

    @v.route("/users/{user_id}")
    def get_user(user_id: int, format: str = "json"):
        return {"id": user_id, "format": format}

    @v.route("/search")
    def search(q: str = "", limit: int = 10):
        return {"q": q, "limit": limit}

    @v.route("/echo")
    def echo(name: str = "World"):
        return {"hello": name}

    @v.route("/ids")
    def ids(ids: list[int]):
        return {"ids": ids}

    @v.route("/tags")
    def tags(tags: list[str]):
        return {"tags": tags}

    @v.route("/flags")
    def flags(flags: list[bool]):
        return {"flags": flags}

    @v.route("/mixed")
    def mixed(items: list[int | str]):
        return {"items": items}

    @v.route("/maybe")
    def maybe(n: int | None = None):
        return {"n": n}

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
    r = await client.get("/search?q=ferrox&limit=25")
    assert r.json() == {"q": "ferrox", "limit": 25}


@pytest.mark.asyncio
async def test_default_query_param(client):
    r = await client.get("/search")
    assert r.json() == {"q": "", "limit": 10}


@pytest.mark.asyncio
async def test_echo_default(client):
    r = await client.get("/echo?name=Ferrox")
    assert r.json() == {"hello": "Ferrox"}


@pytest.mark.asyncio
async def test_invalid_path_param_returns_404(client):
    r = await client.get("/users/abc")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_invalid_query_param_returns_400(client):
    r = await client.get("/search?limit=abc")
    assert r.status_code == 400


async def test_list_int_param(client):
    r = await client.get("/ids?ids=1&ids=2&ids=3")
    assert r.json() == {"ids": [1, 2, 3]}


async def test_list_param_missing_is_empty(client):
    r = await client.get("/ids")
    assert r.json() == {"ids": []}


async def test_list_str_param(client):
    r = await client.get("/tags?tags=a&tags=b")
    assert r.json() == {"tags": ["a", "b"]}


async def test_list_bool_param(client):
    r = await client.get("/flags?flags=true&flags=0&flags=no")
    assert r.json() == {"flags": [True, False, False]}


async def test_list_invalid_element_returns_400(client):
    r = await client.get("/ids?ids=1&ids=x")
    assert r.status_code == 400


async def test_union_list_param(client):
    """list[int | str]: int пробуется первым, иначе остаётся строка."""
    r = await client.get("/mixed?items=1&items=abc&items=2.5")
    assert r.json() == {"items": [1, "abc", "2.5"]}


async def test_union_optional_int_param(client):
    assert (await client.get("/maybe?n=7")).json() == {"n": 7}
    assert (await client.get("/maybe")).json() == {"n": None}


async def test_union_optional_invalid_is_400(client):
    r = await client.get("/maybe?n=abc")
    assert r.status_code == 400
