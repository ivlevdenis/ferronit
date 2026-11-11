"""Minimal application: routes, typed parameters, JSON and plain text.

Run:
    cd <repository root>
    .venv/bin/python -m granian --interface asgi --no-ws examples.minimal_app:app
    # or: .venv/bin/python examples/minimal_app.py  (uvicorn, port 8000)

Check:
    curl "localhost:8000/hello?name=Denis"
    curl localhost:8000/items/42
    curl localhost:8000/items/abc            # 404: the path parameter is typed as int
    curl -X POST localhost:8000/echo -H 'content-type: application/json' -d '{"a": 1}'
"""

from __future__ import annotations

from ferronit import Ferronit

app = Ferronit()


@app.route("/")
async def index() -> dict:
    """Service banner."""
    return {"service": "minimal", "status": "ok"}


@app.route("/hello")
async def hello(name: str = "world") -> str:
    """Query parameter with a default — returned as plain text."""
    return f"Hello, {name}!"


@app.route("/items/{item_id}")
async def item(item_id: int) -> dict:
    """Typed path parameter: ``/items/abc`` yields 404 instead of a 500."""
    return {"item_id": item_id, "type": type(item_id).__name__}


@app.route("/echo", methods=["POST"])
async def echo(req) -> dict:
    """Echo the JSON body back, together with the HTTP method."""
    return {"method": req.method, "received": await req.json()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)
