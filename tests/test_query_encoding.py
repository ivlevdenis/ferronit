"""Кодировки в query-строке — регрессия на двойное кодирование.

Клиенты шлют параметры по-разному: curl и часть SDK передают сырые UTF-8 байты,
браузеры — percent-encoded, легаси — latin-1. Все три варианта должны давать
корректный Python-str, а не «ÐÐµÐ½Ð¸Ñ».
"""

import pytest

from ferrox import Ferrox


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
    def echo(req):
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
    """curl шлёт сырые UTF-8 байты — раньше получалось двойное кодирование."""
    app = Ferrox()
    query = await query_of(app, "name=Денис".encode())

    assert query["name"] == ["Денис"]


@pytest.mark.asyncio
async def test_percent_encoded_utf8_is_decoded():
    app = Ferrox()
    query = await query_of(app, b"name=%D0%94%D0%B5%D0%BD%D0%B8%D1%81")

    assert query["name"] == ["Денис"]


@pytest.mark.asyncio
async def test_latin1_bytes_still_work():
    """Настоящая latin-1-строка (é = 0xE9) не должна ломаться."""
    app = Ferrox()
    query = await query_of(app, b"city=caf\xe9")

    assert query["city"] == ["café"]


@pytest.mark.asyncio
async def test_plus_becomes_space_and_broken_percent_is_preserved():
    app = Ferrox()
    query = await query_of(app, b"q=a+b&broken=%zz")

    assert query["q"] == ["a b"]
    assert query["broken"] == ["%zz"]


@pytest.mark.asyncio
async def test_repeated_keys_accumulate_into_list():
    app = Ferrox()
    query = await query_of(app, b"tag=one&tag=two")

    assert query["tag"] == ["one", "two"]


@pytest.mark.asyncio
async def test_cyrillic_path_param_roundtrip_via_example():
    """Сквозная проверка на примере: /hello?name=Денис отдаёт правильный текст."""
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
