# WebSocket

```python
from ferrox import Ferrox, WebSocket, WebSocketState

app = Ferrox()

@app.websocket("/ws")
async def ws(conn: WebSocket):
    await conn.accept()
    while True:
        try:
            msg = await conn.receive()
        except Exception:
            break          # разрыв соединения
        await conn.send(f"echo: {msg}")
```

`receive()` при разрыве соединения **бросает исключение**, а не возвращает
`None` — оборачивайте его в `try/except` и выходите из цикла. (Фреймворк сам
ловит это исключение и закрывает соединение, так что `except Exception` здесь —
корректный идиоматичный способ.)

## Объект соединения

| Член | Примечания |
|---|---|
| `conn.state` | `WebSocketState` (CONNECTING / CONNECTED / DISCONNECTED) |
| `conn.path` | путь рукопожатия |
| `conn.headers` | заголовки рукопожатия (в нижнем регистре) |
| `await conn.accept()` | принять рукопожатие |
| `await conn.receive()` | следующее текстовое/бинарное сообщение; бросает исключение при разрыве |
| `await conn.send(data)` | отправить текст или bytes |
| `await conn.send_json(obj)` | отправить JSON |
| `await conn.receive_json()` | получить и декодировать JSON-сообщение |
| `await conn.close(code=1000)` | закрыть |

## Origin-защита (CSRF поверх WebSocket)

Передайте `origins=`, чтобы отклонять соединения, чей `Origin` не в списке —
соединение закрывается кодом **1008** до запуска хендлера:

```python
@app.websocket("/ws", origins=["https://app.example.com"])
async def ws(conn):
    ...
```
