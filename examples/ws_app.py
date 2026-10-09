"""WebSocket: эхо-сервер с проверкой Origin (защита от CSRF поверх WebSocket).

Origin-guard задаётся прямо в декораторе: соединение с чужого сайта закрывается
кодом 1008 до того, как хендлер начнёт работу.

Запуск:
    .venv/bin/python -m granian --interface asgi examples.ws_app:app
Проверка из браузера (консоль на странице того же origin):
    const ws = new WebSocket("ws://localhost:8000/ws/echo");
    ws.onmessage = e => console.log(e.data);
    ws.onopen = () => ws.send('{"hello": "ferronit"}');
"""

from __future__ import annotations

from ferronit import Ferronit, WebSocket, WebSocketState

app = Ferronit()

# В проде перечисли свои origin: origins=["https://app.example"]
ALLOWED_ORIGINS = ["http://localhost:8000"]


@app.websocket("/ws/echo", origins=ALLOWED_ORIGINS)
async def echo(conn: WebSocket) -> None:
    """Echo every JSON message back until the client disconnects."""
    await conn.accept()
    while conn.state is WebSocketState.CONNECTED:
        try:
            message = await conn.receive_json()
        except Exception:
            # клиент отключился: receive() бросает внутреннее исключение разрыва
            break
        await conn.send_json({"echo": message, "path": conn.path})
    await conn.close()  # no-op, если соединение уже разорвано клиентом


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)
