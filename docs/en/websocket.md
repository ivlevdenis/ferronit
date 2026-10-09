# WebSocket

```python
from ferrox import Ferrox, WebSocket, WebSocketState

app = Ferrox()

@app.websocket("/ws")
async def ws(conn: WebSocket):
    await conn.accept()
    while conn.state == WebSocketState.CONNECTED:
        msg = await conn.receive()
        if msg is None:
            break
        await conn.send(f"echo: {msg}")
```

## Connection object

| Member | Notes |
|---|---|
| `conn.state` | `WebSocketState` (CONNECTING / CONNECTED / DISCONNECTED) |
| `conn.path` | handshake path |
| `conn.headers` | handshake headers (lower-cased) |
| `await conn.accept()` | accept the handshake |
| `await conn.receive()` | next text/binary message (or `None` on disconnect) |
| `await conn.send(data)` | send text or bytes |
| `await conn.send_json(obj)` | send JSON |
| `await conn.close(code=1000)` | close |

## Origin guard (CSRF over WebSocket)

Pass `origins=` to reject connections whose `Origin` header is not listed —
the connection is closed with code **1008** before the handler runs:

```python
@app.websocket("/ws", origins=["https://app.example.com"])
async def ws(conn):
    ...
```
