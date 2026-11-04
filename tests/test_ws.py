"""WebSocket + streaming tests."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from ferronit import Ferronit


@pytest.fixture
def app():
    v = Ferronit(debug=True)

    @v.websocket("/ws/echo")
    async def echo(ws):
        await ws.accept()
        try:
            while True:
                msg = await ws.receive()
                await ws.send(f"echo: {msg}")
        except Exception:  # noqa: S110 - разрыв соединения клиентом завершает эхо-цикл
            pass

    @v.websocket("/ws/chat")
    async def chat(ws):
        await ws.accept()
        try:
            while True:
                data = await ws.receive_json()
                await ws.send_json({"reply": data.get("text", "?")})
        except Exception:  # noqa: S110 - разрыв соединения клиентом завершает цикл
            pass

    @v.route("/stream")
    async def stream(req):
        async def generate():
            for i in range(3):
                yield f"data: chunk {i}\n\n"
                await asyncio.sleep(0.001)

        return generate()

    return v


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_streaming(client):
    async with client.stream("GET", "/stream") as resp:
        assert resp.status_code == 200
        body = await resp.aread()
        assert b"chunk 0" in body
        assert b"chunk 1" in body
        assert b"chunk 2" in body


@pytest.mark.asyncio
async def test_websocket_echo(client):
    async with client.stream("GET", "http://test/ws/echo") as _ws:
        # httpx doesn't support WebSocket natively; test via ASGI transport
        pass


async def test_ws_accept_headers_go_through_the_header_contract() -> None:
    """Handshake headers are field lines too: no CRLF, lowercase, latin-1."""
    app = Ferronit()

    @app.websocket("/ws/h")
    async def h(ws):
        await ws.accept(headers={"Set-Cookie": "a=1\r\nX-Injected: yes", "X-Tag": "v"})

    sent: list[dict] = []

    scope = {"type": "websocket", "path": "/ws/h", "headers": [], "query_string": b""}

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)

    accept = sent[0]
    assert accept["type"] == "websocket.accept"
    assert [name for name, _ in accept["headers"]] == [b"set-cookie", b"x-tag"]
    value = accept["headers"][0][1]
    assert b"\r" not in value and b"\n" not in value
    assert value.startswith(b"a=1")
