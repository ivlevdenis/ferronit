"""Request object — Rust-native parsing for headers, query, JSON."""

from __future__ import annotations

from velox.contrib.pydantic import decode_json
from velox_core import Request as RustRequest

__all__ = ["Request", "RequestError", "BodyTooLarge", "PathParamError"]


class RequestError(Exception):
    """Клиентская ошибка запроса (invalid body/JSON) → HTTP 400."""


class BodyTooLarge(Exception):
    """Тело запроса превышает max_body_size → HTTP 413."""


class PathParamError(Exception):
    """Path-параметр не прошёл типизацию → HTTP 404 (ASVS 2.1.1)."""


class Request:
    """ASGI request — delegates parsing to Rust where possible."""

    __slots__ = ("_scope", "_receive", "_rust", "_params", "_headers_cache")

    def __init__(self, scope: dict, receive, path_params: dict[str, str] | None = None) -> None:
        self._scope = scope
        self._receive = receive
        self._params: dict[str, str] = path_params or scope.get("route_params", {})
        self._headers_cache: dict[str, str] | None = None

        method = scope.get("method", "GET")
        path = scope.get("path", "/")
        qs = scope.get("query_string", b"")
        if isinstance(qs, bytes):
            qs = qs.decode("latin-1")

        self._rust = RustRequest(method, path, qs)
        raw_headers = scope.get("headers", [])
        if raw_headers:
            try:
                if isinstance(raw_headers[0][0], bytes):
                    self._rust.set_headers(list(raw_headers))
                else:
                    self._rust.set_headers([(bytes(k), bytes(v)) for k, v in raw_headers])
            except Exception:
                pass

    @property
    def params(self) -> dict[str, str]:
        return self._params

    @property
    def method(self) -> str:
        return self._rust.method

    @property
    def path(self) -> str:
        return self._rust.path

    @property
    def headers(self) -> dict[str, str]:
        if self._headers_cache is None:
            self._headers_cache = self._rust.headers
        return self._headers_cache

    def get_header(self, name: str) -> str | None:
        """Быстрый поиск одного заголовка без полного парсинга (Rust)."""
        return self._rust.get_header(name)

    @property
    def query(self) -> dict[str, list[str]]:
        return self._rust.query

    async def body(self) -> bytes:
        chunks: list[bytes] = []
        total = 0
        max_size = self._scope.get("velox.max_body_size")
        more = True
        while more:
            msg = await self._receive()
            more = msg.get("more_body", False)
            chunk = msg.get("body", b"")
            if chunk:
                chunks.append(chunk)
                total += len(chunk)
                if max_size is not None and total > max_size:
                    raise BodyTooLarge(f"Body exceeds {max_size} bytes")
        body = b"".join(chunks)
        self._rust.set_body(body)
        return body

    async def json(self) -> object:
        await self.body()
        try:
            return self._rust.json()
        except ValueError as e:
            raise RequestError(f"Invalid JSON: {e}") from None

    async def model(self, target: type):
        return decode_json(await self.json(), target)

    async def form(self) -> dict[str, list[str]]:
        from urllib.parse import parse_qs
        body = await self.body()
        return parse_qs(body.decode("latin-1"))
