"""In-process вызов Ferronit по **RSGI** — без ASGI и без granian.

Ferronit в профилях и микробенчах всегда гоняется через ``__rsgi__``: это его
production-транспорт (``granian --interface rsgi``), и ASGI-вход ``__call__``
имеет другой hot path (два ``send``-события вместо одного ``response_bytes``,
другое построение запроса). Мерить Ferronit через ASGI нельзя — получается не
тот путь.

Классы ниже — минимальные аналоги нативных ``rsgi.Scope`` / ``rsgi.Protocol``:
``Scope.headers.items()`` отдаёт str-пары, ``proto.response_bytes(status,
headers, body)`` собирает ответ, ``await proto()`` отдаёт тело запроса.
"""

from __future__ import annotations

from typing import Any


class Headers:
    """Аналог ``rsgi.Headers``: ``items()`` отдаёт пары строк."""

    def __init__(self, items: Any = ()) -> None:
        self._items = list(items)

    def items(self) -> list[tuple[str, str]]:
        return list(self._items)


class Scope:
    """Аналог нативного RSGI scope (method / path / query_string / headers)."""

    def __init__(
        self,
        method: str = "GET",
        path: str = "/",
        query_string: str = "",
        headers: Any = (),
    ) -> None:
        self.method = method
        self.path = path
        self.query_string = query_string
        self.headers = Headers(headers)
        self.scheme = "http"
        self.client = "127.0.0.1"
        self.server = "127.0.0.1:8000"


class Proto:
    """Аналог RSGI-протокола: собирает ответ и отдаёт тело запроса."""

    __slots__ = ("_body", "body", "headers", "status")

    def __init__(self, body: bytes = b"") -> None:
        self.status: int | None = None
        self.headers: list[tuple[str, str]] = []
        self.body: bytes = b""
        self._body = body

    def response_bytes(self, status: int, headers: Any, body: bytes) -> None:
        """Принять ответ (единственный вызов на запрос, как в RSGI)."""
        self.status = status
        self.headers = list(headers)
        self.body = body

    async def __call__(self) -> bytes:
        """Тело запроса (RSGI отдаёт его одним вызовом)."""
        return self._body


async def call(
    app,
    *,
    method: str = "GET",
    path: str = "/",
    query_string: str = "",
    headers: Any = (),
    body: bytes = b"",
) -> Proto:
    """Прогнать один запрос через ``app.__rsgi__`` и вернуть ``Proto`` с ответом."""
    proto = Proto(body)
    await app.__rsgi__(Scope(method, path, query_string, headers), proto)
    return proto
