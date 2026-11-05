"""Type stubs for the Ferronit Rust core (``ferronit._core``).

Собирается maturin-ом вместе с расширением: файл попадает в wheel, поэтому mypy
и IDE видят типы нативного ядра, а не только расширение без аннотаций.
"""

from collections.abc import Iterator
from typing import Any

__all__ = [
    "FerronitApp",
    "Headers",
    "Request",
    "Router",
    "build_set_cookie",
    "parse_headers",
]


def build_set_cookie(
    name: str,
    value: str = ...,
    *,
    path: str | None = ...,
    domain: str | None = ...,
    max_age: int | None = ...,
    expires: str | None = ...,
    http_only: bool = ...,
    secure: bool = ...,
    same_site: str | None = ...,
) -> str:
    """Serialise a ``Set-Cookie`` field value.

    Validates RFC 6265 §4.1.1 parts (token name, ``cookie-octet`` value, no CTL
    or ``;`` in path/domain/expires) and raises ``ValueError`` on a violation.
    """


def first_header(raw: Any, name: str) -> str | None:
    """First value of a header in raw pairs or an object with ``items()``."""


def parse_headers(raw: Any) -> dict[str, str]:
    """Build the RFC-combined header dict from raw transport pairs.

    Repeats are joined with ``", "``; ``cookie`` with ``"; "``; ``set-cookie``
    keeps its first line.
    """


class Router:
    """Router split by HTTP method (a matchit router per method)."""

    def __init__(self) -> None: ...
    def add(self, method: str, pattern: str, handler: Any) -> None:
        """Register ``handler`` for ``method``/``pattern``; raises ValueError on a bad pattern."""

    def lookup(
        self, method: str, path: str
    ) -> tuple[Any, dict[str, str] | None] | None:
        """Return ``(handler, params)`` or ``None``.

        ``params`` is ``None`` when the route has no path parameters — the common
        case, which avoids allocating a dict per request.
        """

class Headers:
    """Ordered field lines with duplicates preserved (RFC 9110 §5.3)."""

    def __init__(self, items: Any = ...) -> None: ...
    def add(self, name: str, value: str) -> None: ...
    def _prepend(self, name: str, value: str) -> None: ...
    def get(self, name: str, default: str | None = ...) -> str | None: ...
    def get_all(self, name: str) -> list[str]: ...
    def setdefault(self, name: str, value: str) -> str: ...
    def pop(self, name: str, default: str | None = ...) -> str | None: ...
    def keys(self) -> list[str]: ...
    def values(self) -> list[str]: ...
    def items(self) -> list[tuple[str, str]]: ...
    def raw(self) -> list[tuple[bytes, bytes]]: ...
    def str_pairs(self) -> list[tuple[str, str]]: ...
    def __getitem__(self, name: str) -> str: ...
    def __setitem__(self, name: str, value: str) -> None: ...
    def __delitem__(self, name: str) -> None: ...
    def __contains__(self, name: object) -> bool: ...
    def __iter__(self) -> Iterator[tuple[str, str]]: ...
    def __len__(self) -> int: ...


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
        """Lowercased name → value, repeats combined per RFC (built on first access).

        Repeats are joined in wire order with ``", "``; ``cookie`` uses ``"; "``
        (RFC 9113 §8.2.3) and ``set-cookie`` keeps its first line (RFC 6265).
        """

    @property
    def query(self) -> dict[str, list[str]]:
        """URL-decoded query params; every value is a list (repeated keys accumulate)."""

    def set_headers(self, raw_headers: Any) -> None:
        """Store the transport pairs (bytes on ASGI, str on RSGI) without parsing."""

    def get_header(self, name: str) -> str | None:
        """First value of a header (case-insensitive), no dict build."""

    def get_all(self, name: str) -> list[str]:
        """Every value of a repeated header in wire order."""

    @property
    def cookies(self) -> dict[str, str]:
        """Parsed ``Cookie`` fields; repeats joined with ``"; "`` before parsing."""

    def set_body(self, body: bytes) -> None: ...

    def parse_multipart(
        self, boundary: str
    ) -> tuple[dict[str, list[str]], dict[str, list[tuple[str, str, bytes]]]]:
        """Parse a multipart/form-data body: (fields, files)."""

class FerronitApp:
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
