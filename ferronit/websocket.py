"""WebSocket support — standards-compliant ASGI WebSocket handling."""

from __future__ import annotations

from enum import Enum

__all__ = ["WebSocket", "WebSocketState"]


class WebSocketState(Enum):
    """Lifecycle state of a :class:`WebSocket` connection.

    Attributes:
        CONNECTING: Handshake not yet accepted.
        CONNECTED: Handshake accepted; messages may flow.
        DISCONNECTED: Connection closed or the peer disconnected.
    """

    CONNECTING = 0
    CONNECTED = 1
    DISCONNECTED = 2


class WebSocket:
    """ASGI WebSocket connection — text, binary and JSON messaging.

    The application builds one per connection and passes it to the handler.
    Call :meth:`accept` before sending, then use :meth:`receive` / :meth:`send`
    (or the JSON helpers) until the peer disconnects.

    Attributes:
        state: Current :class:`WebSocketState`.
        path: Path of the WebSocket handshake.
        headers: Handshake headers with lower-cased keys.
    """

    __slots__ = ("_receive", "_scope", "_send", "_state")

    def __init__(self, scope: dict, receive, send) -> None:
        self._scope = scope
        self._receive = receive
        self._send = send
        self._state = WebSocketState.CONNECTING

    @property
    def state(self) -> WebSocketState:
        """WebSocketState: Current lifecycle state of the connection."""
        return self._state

    @property
    def path(self) -> str:
        """str: Path of the WebSocket handshake request."""
        return self._scope.get("path", "/")

    @property
    def headers(self) -> dict[str, str]:
        """dict[str, str]: Handshake headers with lower-cased keys."""
        raw: list[tuple[bytes, bytes]] = self._scope.get("headers", [])
        return {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in raw
        }

    async def accept(self, headers: dict[str, str] | None = None) -> None:
        """Accept the handshake and mark the connection as connected.

        Args:
            headers: Extra response headers to send with ``websocket.accept``.
        """
        extra: list[tuple[bytes, bytes]] = []
        if headers:
            for k, v in headers.items():
                extra.append((k.encode("latin-1"), v.encode("latin-1")))
        await self._send({"type": "websocket.accept", "headers": extra} if extra else {"type": "websocket.accept"})
        self._state = WebSocketState.CONNECTED

    async def receive(self) -> str | bytes:
        """Receive one message from the peer.

        Returns:
            str | bytes: Text payload as ``str``; binary payload as ``bytes``.

        Raises:
            _WebSocketDisconnect: When the client disconnects (code defaults to
                1000); the state becomes :attr:`WebSocketState.DISCONNECTED`.
            RuntimeError: On an unexpected ASGI message type.
        """
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
        """Send a text or binary message.

        Args:
            data: Payload; ``str`` is sent as a text frame, ``bytes`` as binary.
        """
        if isinstance(data, str):
            await self._send({"type": "websocket.send", "text": data})
        else:
            await self._send({"type": "websocket.send", "bytes": data})

    async def send_json(self, data: object) -> None:
        """Serialise ``data`` to compact JSON and send it as a text message.

        Args:
            data: Any JSON-serialisable value.
        """
        import json

        await self.send(json.dumps(data, ensure_ascii=False, separators=(",", ":")))

    async def receive_json(self) -> object:
        """Receive one message and decode it as JSON.

        Returns:
            object: The decoded JSON value.
        """
        import json

        msg = await self.receive()
        if isinstance(msg, str):
            return json.loads(msg)
        return json.loads(msg.decode())

    async def close(self, code: int = 1000) -> None:
        """Close the connection, unless it is already disconnected.

        Args:
            code: WebSocket close code sent to the peer.
        """
        if self._state == WebSocketState.DISCONNECTED:
            return
        await self._send({"type": "websocket.close", "code": code})
        self._state = WebSocketState.DISCONNECTED


class _WebSocketDisconnect(Exception):
    pass
