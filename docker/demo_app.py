"""Минимальное приложение для проверки образа Ferronit.

Запускается в контейнере по умолчанию (APP=demo_app:app): без БД и внешних
зависимостей, поэтому годится и для smoke-теста образа, и как отправная точка.

Свой проект подключается через монтирование и APP:
    docker run --rm -p 8000:8000 -v "$PWD:/app" -e APP=app:app ferronit:0.10.0
"""

from __future__ import annotations

import platform

import ferronit

app = ferronit.Ferronit()


@app.route("/")
def index() -> dict:
    return {
        "service": "ferronit",
        "status": "ok",
        "version": ferronit.__version__,
        "python": platform.python_version(),
    }


@app.route("/health")
def health() -> dict:
    return {"status": "ok"}


@app.route("/json")
def payload(n: int = 10) -> dict:
    """JSON-эндпоинт: сериализация идёт через Rust (serde_json)."""
    return {"count": n, "items": [{"id": i, "name": f"item-{i}"} for i in range(n)]}


@app.route("/echo", methods=["POST"])
async def echo(req: ferronit.Request) -> dict:
    body = await req.json()
    return {"received": body}
