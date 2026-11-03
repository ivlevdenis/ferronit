"""Routing tests — static routes, parameterised routes, 404."""

import pytest
from httpx import ASGITransport, AsyncClient

from ferronit import Ferronit


@pytest.fixture
def app():
    v = Ferronit(debug=True)

    @v.route("/users/{user_id}")
    async def get_user(req):
        return {"user_id": req.params["user_id"]}

    @v.route("/posts/{post_id}/comments/{comment_id}")
    async def get_comment(req):
        return {"post_id": req.params["post_id"], "comment_id": req.params["comment_id"]}

    @v.route("/")
    async def home(req):
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


def test_sync_http_handler_is_rejected_at_registration():
    """Sync route handlers are rejected before they can block the event loop."""
    app = Ferronit()

    with pytest.raises(TypeError, match=r"'sync_handler'.*async"):

        @app.route("/sync")
        def sync_handler(req):
            return {"ok": True}


def test_sync_websocket_handler_is_rejected_at_registration():
    app = Ferronit()

    with pytest.raises(TypeError, match=r"'sync_ws'.*async"):

        @app.websocket("/ws")
        def sync_ws(ws):
            return None


def test_async_callable_object_handler_is_accepted():
    """HealthCheck-style objects expose an async ``__call__`` and must register."""

    class Handler:
        async def __call__(self, req):
            return {"ok": True}

    app = Ferronit()
    app.route("/callable")(Handler())


def test_route_with_26_params_is_rejected_with_value_error():
    """Шаблон из 26 параметров matchit не раскладывает по буквам a..z и паникует.

    Наружу это обязано выходить ValueError («Invalid route: ...»), а не
    PanicException: паника Rust через FFI — не тот отказ, который ловит код
    приложения (PanicException наследуется от BaseException).
    """
    v = Ferronit()
    pattern = "/" + "/".join(f"{{p{i}}}" for i in range(26))

    async def dummy(req):
        return {}

    with pytest.raises(ValueError, match="Invalid route"):
        v.route(pattern)(dummy)


def test_route_with_25_params_is_accepted():
    """25 параметров — предел matchit, шаблон ещё регистрируется."""
    v = Ferronit()
    pattern = "/" + "/".join(f"{{p{i}}}" for i in range(25))

    async def dummy(req):
        return {}

    v.route(pattern)(dummy)
