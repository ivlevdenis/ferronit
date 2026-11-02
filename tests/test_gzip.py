"""gzip: мелкие тела не сжимаются, крупные — сжимаются (оба транспорта).

Фиксированная цена zlib — ~40 мкс на вызов, поэтому сжатие 11-байтного JSON
делало ответ и медленнее, и больше. Порог — ``Ferronit(gzip_min_size=500)``.
"""

from __future__ import annotations

import gzip

from ferronit import Ferronit


def _app(**kwargs) -> Ferronit:
    app = Ferronit(**kwargs)

    @app.route("/small")
    async def small():
        return {"ok": True}

    @app.route("/big")
    async def big():
        return {"rows": [{"id": i, "name": "x" * 20} for i in range(100)]}

    return app


async def _asgi(app: Ferronit, path: str):
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": b"",
        "headers": [(b"host", b"t"), (b"accept-encoding", b"gzip")],
        "scheme": "http",
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8000),
        "route_params": {},
    }
    out: dict = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
            # заголовки сравниваем в нижнем регистре: Content-Encoding приходит с большой
            out["headers"] = {k.decode().lower(): v for k, v in message["headers"]}
        else:
            out["body"] = message["body"]

    await app(scope, receive, send)
    return out


async def _rsgi(app: Ferronit, path: str):
    proto = _Proto()
    await app.__rsgi__(_Scope(path=path), proto)
    return {
        "status": proto.status,
        "headers": {k.lower(): v for k, v in proto.headers},
        "body": proto.body,
    }


class _Headers:
    def __init__(self, items) -> None:
        self._items = items

    def items(self):
        return list(self._items)


class _Scope:
    def __init__(self, path: str) -> None:
        self.method = "GET"
        self.path = path
        self.query_string = ""
        self.headers = _Headers(
            [("host", "t"), ("accept-encoding", "gzip")]
        )


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


async def test_small_body_is_not_gzipped() -> None:
    out = await _asgi(_app(), "/small")
    assert "content-encoding" not in out["headers"]
    assert out["body"] == b'{"ok":true}'


async def test_large_body_is_gzipped() -> None:
    out = await _asgi(_app(), "/big")
    assert out["headers"]["content-encoding"] == b"gzip"
    assert gzip.decompress(out["body"]).startswith(b'{"rows"')


async def test_gzip_min_size_zero_compresses_small_bodies() -> None:
    out = await _asgi(_app(gzip_min_size=0), "/small")
    assert out["headers"]["content-encoding"] == b"gzip"


async def test_rsgi_small_body_is_not_gzipped() -> None:
    out = await _rsgi(_app(), "/small")
    assert "content-encoding" not in out["headers"]
    assert out["body"] == b'{"ok":true}'


async def test_rsgi_large_body_is_gzipped() -> None:
    out = await _rsgi(_app(), "/big")
    assert out["headers"]["content-encoding"] == "gzip"
    assert gzip.decompress(out["body"]).startswith(b'{"rows"')
