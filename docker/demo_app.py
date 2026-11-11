"""Minimal application for testing the Ferronit image.

Runs in the container by default (APP=demo_app:app): no DB and no external
dependencies, so it suits both an image smoke test and a starting point.

Your own project is plugged in via a mount and APP:
    docker run --rm -p 8000:8000 -v "$PWD:/app" -e APP=app:app ferronit:0.24.0
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
    """JSON endpoint: serialisation goes through msgspec (C fast path)."""
    return {"count": n, "items": [{"id": i, "name": f"item-{i}"} for i in range(n)]}


@app.route("/echo", methods=["POST"])
async def echo(req: ferronit.Request) -> dict:
    body = await req.json()
    return {"received": body}
