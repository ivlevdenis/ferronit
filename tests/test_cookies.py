"""`Request.cookies` / `get_cookie`: разбор Cookie-заголовка (lazy, cached)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from ferrox import Ferrox


@pytest.fixture
def app():
    v = Ferrox()

    @v.route("/cookies")
    async def cookies(req):
        return {"all": req.cookies, "theme": req.get_cookie("theme", "light")}

    return v


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_cookies_parsed(client) -> None:
    r = await client.get("/cookies", headers={"cookie": "theme=dark; session=abc123"})
    assert r.json() == {"all": {"theme": "dark", "session": "abc123"}, "theme": "dark"}


async def test_cookies_quoted_and_whitespace(client) -> None:
    r = await client.get("/cookies", headers={"cookie": 'a=1; b="hello world"; c = 3'})
    assert r.json()["all"] == {"a": "1", "b": "hello world", "c": "3"}


async def test_cookies_empty(client) -> None:
    r = await client.get("/cookies")
    assert r.json() == {"all": {}, "theme": "light"}


async def test_cookies_malformed_pairs_skipped(client) -> None:
    r = await client.get("/cookies", headers={"cookie": "novalue; =x; good=1"})
    assert r.json()["all"] == {"good": "1"}
