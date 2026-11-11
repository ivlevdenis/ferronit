"""WebSocket: echo server with an Origin check (CSRF protection over WebSocket).

The origin guard is set right in the decorator: a connection from a foreign site
is closed with code 1008 before the handler starts running.

Run:
    .venv/bin/python -m granian --interface asgi examples.ws_app:app
Check from a browser (console on a page of the same origin):
    const ws = new WebSocket("ws://localhost:8000/ws/echo");
    ws.onmessage = e => console.log(e.data);
    ws.onopen = () => ws.send('{"hello": "ferronit"}');
"""

from __future__ import annotations

from ferronit import Ferronit, WebSocket, WebSocketState

app = Ferronit()

# In production list your own origins: origins=["https://app.example"]
ALLOWED_ORIGINS = ["http://localhost:8000"]


@app.websocket("/ws/echo", origins=ALLOWED_ORIGINS)
async def echo(conn: WebSocket) -> None:
    """Echo every JSON message back until the client disconnects."""
    await conn.accept()
    while conn.state is WebSocketState.CONNECTED:
        try:
            message = await conn.receive_json()
        except Exception:
            # the client disconnected: receive() raises an internal disconnect exception
            break
        await conn.send_json({"echo": message, "path": conn.path})
    await conn.close()  # no-op if the client has already torn the connection down


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)
