"""RSGI-транспорт (granian): запрос должен видеть заголовки и query.

Регрессия: ``_rsgi_request`` передавал заголовки строками, а ``Request`` ждал
байты — конвертация падала и глушилась, поэтому ``request.headers`` был пуст и
любая аутентификация/куки на RSGI не работали.
"""

from __future__ import annotations

from ferronit import Ferronit


class _Headers:
    """Минимальный аналог ``rsgi.Headers``: ``items()`` отдаёт str-пары."""

    def __init__(self, items: list[tuple[str, str]]) -> None:
        self._items = items

    def items(self) -> list[tuple[str, str]]:
        return list(self._items)


class _Scope:
    def __init__(
        self,
        *,
        method: str = "GET",
        path: str = "/me",
        query_string: str = "",
        headers: list[tuple[str, str]] | None = None,
    ) -> None:
        self.method = method
        self.path = path
        self.query_string = query_string
        self.headers = _Headers(headers or [])


class _Proto:
    """Минимальный аналог RSGI-протокола: собранный ответ + тело запроса."""

    def __init__(self, body: bytes = b"") -> None:
        self.status: int | None = None
        self.headers: list[tuple[str, str]] = []
        self.body: bytes | None = None
        self._body = body

    def response_bytes(self, status: int, headers, body: bytes) -> None:
        self.status = status
        self.headers = list(headers)
        self.body = body

    async def __call__(self) -> bytes:
        return self._body


async def test_rsgi_headers_reach_request() -> None:
    app = Ferronit()
    seen: dict[str, str] = {}

    @app.route("/me")
    async def me(req):
        seen.update(req.headers)
        return {"ok": True}

    proto = _Proto()
    await app.__rsgi__(
        _Scope(headers=[("authorization", "Bearer token-1"), ("host", "bench")]), proto
    )

    assert proto.status == 200
    assert seen["authorization"] == "Bearer token-1"
    assert seen["host"] == "bench"


async def test_rsgi_query_reaches_request() -> None:
    app = Ferronit()
    seen: dict[str, list[str]] = {}

    @app.route("/rows")
    async def rows(req):
        seen.update(req.query)
        return {"ok": True}

    proto = _Proto()
    await app.__rsgi__(_Scope(path="/rows", query_string="limit=7"), proto)

    assert proto.status == 200
    assert seen["limit"] == ["7"]


async def test_get_header_is_case_insensitive() -> None:
    app = Ferronit()
    seen: dict[str, str | None] = {}

    @app.route("/h")
    async def h(req):
        seen["lower"] = req.get_header("authorization")
        seen["upper"] = req.get_header("Authorization")
        seen["missing"] = req.get_header("x-nope")
        return {"ok": True}

    proto = _Proto()
    await app.__rsgi__(_Scope(path="/h", headers=[("Authorization", "Bearer t")]), proto)

    assert seen == {"lower": "Bearer t", "upper": "Bearer t", "missing": None}


async def test_duplicate_headers_combine_in_dict() -> None:
    """get_header — первое значение, headers — склейка в порядке провода."""
    app = Ferronit()
    seen: dict[str, object] = {}

    @app.route("/h")
    async def h(req):
        seen["value"] = req.get_header("x-tag")
        seen["dict"] = req.headers["x-tag"]
        return {"ok": True}

    proto = _Proto()
    await app.__rsgi__(
        _Scope(path="/h", headers=[("x-tag", "first"), ("x-tag", "second")]), proto
    )

    assert seen == {"value": "first", "dict": "first, second"}
