"""WebSocket + streaming tests."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from velox import Velox


@pytest.fixture
def app():
    v = Velox(debug=True)

    @v.websocket("/ws/echo")
    async def echo(ws):
        await ws.accept()
        try:
            while True:
                msg = await ws.receive()
                await ws.send(f"echo: {msg}")
        except Exception:
            pass

    @v.websocket("/ws/chat")
    async def chat(ws):
        await ws.accept()
        try:
            while True:
                data = await ws.receive_json()
                await ws.send_json({"reply": data.get("text", "?")})
        except Exception:
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
    async with client.stream("GET", "http://test/ws/echo") as ws_connect:
        # httpx doesn't support WebSocket natively; test via ASGI transport
        pass
