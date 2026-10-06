"""Примеры из examples/ действительно запускаются — прогон через ASGI in-process.

WebSocket проверяется прямым ASGI-диалогом (httpx не умеет WS), поэтому тест
заодно фиксирует контракт handshake: accept → сообщения → close.
"""

import importlib
import json
import pathlib
import sys

import pytest
from httpx import ASGITransport, AsyncClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_app(module: str):
    """Import an example module and return its ASGI application."""
    return importlib.import_module(f"examples.{module}").app


def client_for(app, **kwargs) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", **kwargs)


# ── minimal_app ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_minimal_app_serves_text_json_and_typed_params():
    app = load_app("minimal_app")
    async with client_for(app) as client:
        assert (await client.get("/")).json()["service"] == "minimal"
        assert (await client.get("/hello?name=Денис")).text == "Hello, Денис!"
        assert (await client.get("/items/42")).json()["item_id"] == 42
        assert (await client.get("/items/abc")).status_code == 404
        echoed = await client.post("/echo", json={"a": 1})
        assert echoed.json() == {"method": "POST", "received": {"a": 1}}


# ── di_app ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_di_app_wires_ports_by_annotation():
    app = load_app("di_app")
    async with client_for(app) as client:
        first = await client.get("/greet/Денис")
        second = await client.get("/greet/Денис")

    assert first.json()["greeting"] == "Привет, Денис!"
    assert second.json()["greeting"] == first.json()["greeting"]  # кэш-порт вернул то же
    async with client_for(app) as client:
        assert (await client.get("/stats")).json()["served"] == 2


# ── llm_sse_app ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_app_returns_whole_answer():
    app = load_app("llm_sse_app")
    async with client_for(app) as client:
        response = await client.post("/chat", json={"prompt": "что такое Ferrox"})

    body = response.json()
    assert body["model"] == "mock/v1"
    assert body["reply"]


@pytest.mark.asyncio
async def test_llm_app_streams_sse_chunks():
    app = load_app("llm_sse_app")
    async with client_for(app) as client:
        response = await client.post("/chat/stream", json={"prompt": "стриминг"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "data: " in response.text
    assert response.text.rstrip().endswith("data: [DONE]")


# ── rag_app ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rag_app_retrieves_relevant_document():
    app = load_app("rag_app")
    async with client_for(app) as client:
        response = await client.post("/ask", json={"question": "rust роутинг"})

    body = response.json()
    assert body["sources"][0]["id"] == "routing"  # локальный векторизатор реально ранжирует
    assert body["answer"]


# ── ws_app ─────────────────────────────────────────────────────────────

async def drive_websocket(app, path: str, messages: list[dict], origin: str = ""):
    """Run one WebSocket session against the ASGI app and collect sent messages."""
    inbox = list(messages)
    sent: list[dict] = []
    headers = [(b"origin", origin.encode())] if origin else []

    async def receive():
        if inbox:
            return inbox.pop(0)
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message):
        sent.append(message)

    await app({"type": "websocket", "path": path, "headers": headers, "query_string": b""},
              receive, send)
    return sent


@pytest.mark.asyncio
async def test_ws_app_echoes_json_for_allowed_origin():
    app = load_app("ws_app")
    sent = await drive_websocket(
        app,
        "/ws/echo",
        [{"type": "websocket.receive", "text": json.dumps({"hello": "ferrox"})},
         {"type": "websocket.disconnect", "code": 1000}],
        origin="http://localhost:8000",
    )

    assert sent[0]["type"] == "websocket.accept"
    echoed = json.loads(sent[1]["text"])
    assert echoed == {"echo": {"hello": "ferrox"}, "path": "/ws/echo"}
    # клиент отключился сам — лишних кадров нет, close() после разрыва ничего не шлёт
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_ws_app_rejects_foreign_origin_before_handler():
    app = load_app("ws_app")
    sent = await drive_websocket(app, "/ws/echo", [], origin="https://evil.example")

    assert sent == [{"type": "websocket.close", "code": 1008}]
