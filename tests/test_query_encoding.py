"""Query-string encodings — regression against double encoding.

Clients send parameters differently: curl and some SDKs pass raw UTF-8 bytes,
browsers send percent-encoded values, legacy clients use latin-1. All three
variants must yield a correct Python str, not mojibake.
"""

import pytest

from ferronit import Ferronit


def make_scope(query_string: bytes) -> dict:
    return {
        "type": "http",
        "method": "GET",
        "path": "/q",
        "query_string": query_string,
        "headers": [],
        "root_path": "",
        "http_version": "1.1",
        "scheme": "http",
    }


async def query_of(app, query_string: bytes) -> dict:
    """Drive one request through ASGI and return the parsed query of the handler."""
    captured: dict = {}

    @app.route("/q")
    async def echo(req):
        captured.update(req.query)
        return {"ok": True}

    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(make_scope(query_string), receive, send)
    return captured


@pytest.mark.asyncio
async def test_raw_utf8_bytes_are_decoded():
    """curl sends raw UTF-8 bytes — this used to result in double encoding."""
    app = Ferronit()
    query = await query_of(app, "name=Денис".encode())

    assert query["name"] == ["Денис"]


@pytest.mark.asyncio
async def test_percent_encoded_utf8_is_decoded():
    app = Ferronit()
    query = await query_of(app, b"name=%D0%94%D0%B5%D0%BD%D0%B8%D1%81")

    assert query["name"] == ["Денис"]


@pytest.mark.asyncio
async def test_latin1_bytes_still_work():
    """A genuine latin-1 string (é = 0xE9) must not break."""
    app = Ferronit()
    query = await query_of(app, b"city=caf\xe9")

    assert query["city"] == ["café"]


@pytest.mark.asyncio
async def test_plus_becomes_space_and_broken_percent_is_preserved():
    app = Ferronit()
    query = await query_of(app, b"q=a+b&broken=%zz")

    assert query["q"] == ["a b"]
    assert query["broken"] == ["%zz"]


@pytest.mark.asyncio
async def test_repeated_keys_accumulate_into_list():
    app = Ferronit()
    query = await query_of(app, b"tag=one&tag=two")

    assert query["tag"] == ["one", "two"]


@pytest.mark.asyncio
async def test_cyrillic_path_param_roundtrip_via_example():
    """End-to-end check by example: /hello?name=<non-ASCII> returns the correct text."""
    import importlib
    import pathlib
    import sys

    from httpx import ASGITransport, AsyncClient

    root = pathlib.Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    app = importlib.import_module("examples.minimal_app").app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/hello", params={"name": "Денис"})

    assert response.text == "Hello, Денис!"
