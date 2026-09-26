"""WebSocket support — standards-compliant ASGI WebSocket handling."""

from __future__ import annotations

from enum import Enum

__all__ = ["WebSocket", "WebSocketState"]


class WebSocketState(Enum):
    CONNECTING = 0
    CONNECTED = 1
    DISCONNECTED = 2


class WebSocket:
    """ASGI WebSocket connection — receive/send text, bytes, JSON."""

    __slots__ = ("_scope", "_receive", "_send", "_state")

    def __init__(self, scope: dict, receive, send) -> None:
        self._scope = scope
        self._receive = receive
        self._send = send
        self._state = WebSocketState.CONNECTING

    @property
    def state(self) -> WebSocketState:
        return self._state

    @property
    def path(self) -> str:
        return self._scope.get("path", "/")

    @property
    def headers(self) -> dict[str, str]:
        raw: list[tuple[bytes, bytes]] = self._scope.get("headers", [])
        return {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in raw
        }

    async def accept(self, headers: dict[str, str] | None = None) -> None:
        """Accept the WebSocket connection."""
        extra: list[tuple[bytes, bytes]] = []
        if headers:
            for k, v in headers.items():
                extra.append((k.encode("latin-1"), v.encode("latin-1")))
        await self._send({"type": "websocket.accept", "headers": extra} if extra else {"type": "websocket.accept"})
        self._state = WebSocketState.CONNECTED

    async def receive(self) -> str | bytes:
        """Receive one WebSocket message (str for text, bytes for binary)."""
        msg = await self._receive()
        if msg["type"] == "websocket.receive":
            if "text" in msg:
                return msg["text"]
            return msg.get("bytes", b"")
        if msg["type"] == "websocket.disconnect":
            self._state = WebSocketState.DISCONNECTED
            raise _WebSocketDisconnect(msg.get("code", 1000))
        raise RuntimeError(f"Unexpected WS message: {msg['type']}")

    async def send(self, data: str | bytes) -> None:
        """Send text or binary message."""
        if isinstance(data, str):
            await self._send({"type": "websocket.send", "text": data})
        else:
            await self._send({"type": "websocket.send", "bytes": data})

    async def send_json(self, data: object) -> None:
        """Send JSON-serialised message."""
        import json

        await self.send(json.dumps(data, ensure_ascii=False, separators=(",", ":")))

    async def receive_json(self) -> object:
        """Receive and parse one JSON message."""
        import json

        msg = await self.receive()
        if isinstance(msg, str):
            return json.loads(msg)
        return json.loads(msg.decode())

    async def close(self, code: int = 1000) -> None:
        """Close the WebSocket connection."""
        if self._state == WebSocketState.DISCONNECTED:
            return
        await self._send({"type": "websocket.close", "code": code})
        self._state = WebSocketState.DISCONNECTED


class _WebSocketDisconnect(Exception):
    pass
