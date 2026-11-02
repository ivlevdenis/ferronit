"""Заголовки: дубликаты, пустые значения, несколько Cookie — на обоих транспортах."""

from __future__ import annotations

from ferronit import Ferronit


class _Headers:
    def __init__(self, items) -> None:
        self._items = items

    def items(self):
        return list(self._items)


class _Scope:
    def __init__(self, headers) -> None:
        self.method = "GET"
        self.path = "/h"
        self.query_string = ""
        self.headers = _Headers(headers)


class _Proto:
    def __init__(self) -> None:
        self.status = None
        self.headers: list = []
        self.body = b""

    def response_bytes(self, status, headers, body) -> None:
        self.status = status
        self.headers = list(headers)
        self.body = body

    async def __call__(self) -> bytes:
        return b""


async def _asgi(app: Ferronit, headers: list[tuple[bytes, bytes]]) -> None:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/h",
        "query_string": b"",
        "headers": [(b"host", b"t"), *headers],
        "scheme": "http",
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8000),
        "route_params": {},
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        return None

    await app(scope, receive, send)


def _app(sink: dict) -> Ferronit:
    app = Ferronit()

    @app.route("/h")
    async def h(req):
        sink["headers"] = req.headers
        sink["first"] = req.get_header("x-tag")
        sink["all"] = req.get_all("x-tag")
        sink["cookies"] = req.cookies
        return {"ok": True}

    return app


async def test_duplicate_headers_asgi() -> None:
    seen: dict = {}
    await _asgi(_app(seen), [(b"x-tag", b"a"), (b"x-tag", b"b")])

    assert seen["first"] == "a"          # get_header — первый
    assert seen["headers"]["x-tag"] == "b"  # dict — последний
    assert seen["all"] == ["a", "b"]     # get_all — все, в порядке провода


async def test_duplicate_headers_rsgi() -> None:
    seen: dict = {}
    proto = _Proto()
    await _app(seen).__rsgi__(
        _Scope([("host", "t"), ("x-tag", "a"), ("x-tag", "b")]), proto
    )

    assert seen["first"] == "a"
    assert seen["headers"]["x-tag"] == "b"
    assert seen["all"] == ["a", "b"]


async def test_multiple_cookie_headers_are_merged() -> None:
    seen: dict = {}
    await _asgi(_app(seen), [(b"cookie", b"a=1"), (b"cookie", b"b=2")])

    assert seen["cookies"] == {"a": "1", "b": "2"}


async def test_empty_header_value_is_empty_string_not_none() -> None:
    seen: dict = {}
    await _asgi(_app(seen), [(b"x-tag", b"")])

    assert seen["first"] == ""
    assert seen["all"] == [""]


async def test_missing_header_gives_empty_list() -> None:
    seen: dict = {}
    await _asgi(_app(seen), [])

    assert seen["first"] is None
    assert seen["all"] == []


async def test_non_ascii_header_value_asgi() -> None:
    """ASGI сохраняет октеты; str-API декодирует их UTF-8 (как и раньше)."""
    seen: dict = {}
    await _asgi(_app(seen), [(b"x-tag", b"\xd0\xbf\xd1\x80")])

    assert seen["all"] == ["пр"]
