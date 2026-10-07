"""Type stubs for the Ferrox Rust core (``ferrox._core``).

Собирается maturin-ом вместе с расширением: файл попадает в wheel, поэтому mypy
и IDE видят типы нативного ядра, а не только расширение без аннотаций.
"""

from typing import Any

__all__ = ["FerroxApp", "Request", "Response", "Router", "db"]

# Вложенный модуль данных (PostgreSQL → JSON в Rust): connect / query_json.
db: Any

class Router:
    """Conflict-free router over method+path patterns (matchit under the hood)."""

    def __init__(self) -> None: ...
    def add(self, method: str, pattern: str, handler: Any) -> None:
        """Register ``handler`` for ``"{METHOD} {pattern}"``; raises ValueError on a bad pattern."""

    def lookup(
        self, method: str, path: str
    ) -> tuple[Any, dict[str, str] | None] | None:
        """Return ``(handler, params)`` or ``None``.

        ``params`` is ``None`` when the route has no path parameters — the common
        case, which avoids allocating a dict per request.
        """

class Request:
    """Parsed request: method/path/query are Rust-side, headers are parsed lazily."""

    def __init__(self, method: str, path: str, query_string: str) -> None: ...
    @property
    def method(self) -> str: ...
    @property
    def path(self) -> str: ...
    @property
    def query_string(self) -> str: ...
    @property
    def headers(self) -> dict[str, str]:
        """Lowercased header name → value; built on first access."""

    @property
    def query(self) -> dict[str, list[str]]:
        """URL-decoded query params; every value is a list (repeated keys accumulate)."""

    def set_headers(self, raw_headers: list[tuple[bytes, bytes]]) -> None:
        """Store raw header bytes without parsing (fast path)."""

    def get_header(self, name: str) -> str | None:
        """Case-insensitive lookup over raw bytes; `None` when absent."""

    def set_body(self, body: bytes) -> None: ...
    def json(self) -> Any:
        """Parse the body as JSON; raises ValueError on malformed input."""

    def parse_multipart(
        self, boundary: str
    ) -> tuple[dict[str, list[str]], dict[str, list[tuple[str, str, bytes]]]]:
        """Parse a multipart/form-data body: (fields, files)."""

class Response:
    """Rust-built response: status, body bytes and content type."""

    def __init__(self, body: bytes, status: int, content_type: str) -> None: ...
    @property
    def status(self) -> int: ...
    @property
    def body(self) -> bytes: ...
    @property
    def content_type(self) -> str: ...
    @staticmethod
    def json(data: Any, status: int | None = ...) -> Response:
        """Serialize ``data`` with serde_json, escaping HTML-significant characters."""

    @staticmethod
    def text(text: str, status: int | None = ...) -> Response: ...

class FerroxApp:
    """Owns the router and CORS config; resolves routes for the Python ASGI layer."""

    def __init__(self) -> None: ...
    def set_cors(
        self,
        origins: str | None,
        methods: str | None,
        headers: str | None,
        max_age: str | None,
    ) -> None:
        """Configure CORS; ``origins`` is a comma-separated list, empty means ``"*"``."""

    def cors_allows(self, origin: str) -> bool: ...
    def cors_is_wildcard(self) -> bool: ...
    def add_route(self, method: str, path: str, handler: Any) -> None: ...
    def resolve(
        self, method: str, path: str
    ) -> tuple[Any, dict[str, str] | None] | None:
        """Look up a handler for ``method``/``path``."""

    def has_cors(self) -> bool: ...
    def cors_preflight_headers(self, origin: str) -> dict[str, str] | None:
        """Preflight headers when ``origin`` is allowed, else ``None``."""

    def cors_origin(self, origin: str) -> str | None:
        """``Access-Control-Allow-Origin`` value for allowed origins, else ``None``."""

    def gzip_compress(self, data: bytes) -> bytes: ...
    def handle_request(
        self,
        method: str,
        path: str,
        query_string: str,
        raw_headers: list[tuple[bytes, bytes]],
        body: bytes,
    ) -> Response:
        """Route one request and call the Python handler directly (used by benchmarks)."""
