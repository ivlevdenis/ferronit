"""Request object — Rust-native parsing for headers, query, JSON."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ferronit._core import Request as RustRequest
from ferronit.contrib.pydantic import decode_json

__all__ = [
    "BodyTooLarge",
    "PathParamError",
    "Request",
    "RequestError",
    "UnsupportedMediaType",
    "UploadedFile",
]


class RequestError(Exception):
    """Invalid client body or JSON → HTTP 400.

    Raised by :meth:`Request.json` (malformed JSON) and :meth:`Request.model`
    (payload that is not an object or array). The application maps it to a 400
    response.
    """


class BodyTooLarge(Exception):
    """Request body exceeds ``max_body_size`` → HTTP 413.

    Raised by :meth:`Request.body` while streaming chunks, as soon as the
    accumulated size passes ``ferronit.max_body_size`` taken from the ASGI scope.
    The application maps it to a 413 response.
    """


class UnsupportedMediaType(Exception):
    """Content-Type is not supported for the requested operation → HTTP 415.

    Raised by :meth:`Request.form` and :meth:`Request.files` when the body is
    neither urlencoded nor multipart. The application maps it to a 415 response.
    """


class PathParamError(Exception):
    """A path parameter failed type coercion → HTTP 404 (ASVS 2.1.1).

    The application maps it to a 404 so a malformed parameter never reveals
    that the route exists.
    """


def _parse_cookies(header: str | None) -> dict[str, str]:
    """Parse a ``Cookie`` header into ``{name: value}``.

    Handles whitespace around separators and quoted values; malformed pairs
    (no ``=``) are skipped.
    """
    result: dict[str, str] = {}
    if not header:
        return result
    for part in header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        if name:
            result[name] = value
    return result


@dataclass(frozen=True, slots=True)
class UploadedFile:
    """An uploaded file part from a ``multipart/form-data`` request."""

    filename: str
    content: bytes
    content_type: str

    @property
    def size(self) -> int:
        """Size of the uploaded file in bytes."""
        return len(self.content)

    def save(self, path: str | Path) -> None:
        """Write the uploaded file to disk."""
        Path(path).write_bytes(self.content)


def _boundary_from(content_type: str) -> bytes | None:
    """Extract the multipart boundary from a ``Content-Type`` header value."""
    for piece in content_type.split(";"):
        piece = piece.strip()
        if piece.lower().startswith("boundary="):
            boundary = piece.partition("=")[2].strip()
            if len(boundary) >= 2 and boundary[0] == '"' and boundary[-1] == '"':
                boundary = boundary[1:-1]
            return boundary.encode("latin-1")
    return None


class Request:
    """ASGI HTTP request with Rust-native header and query parsing.

    Headers and query are parsed lazily: nothing is parsed until the first
    attribute access, and the result is then cached for the request's lifetime.
    Query values are always lists so repeated keys are preserved.

    Attributes:
        params: Path parameters captured by the route pattern.
        method: HTTP method, e.g. ``"GET"``.
        path: Request path without the query string.
        headers: Request headers, keys lower-cased (parsed lazily).
        query: Query string as ``{name: [values]}`` — every value is a list.
        cookies: Cookies from the ``Cookie`` header (parsed lazily).
    """

    __slots__ = (
        "_cookies_cache",
        "_headers_cache",
        "_multipart_cache",
        "_params",
        "_query_cache",
        "_receive",
        "_rust",
        "_scope",
    )

    def __init__(self, scope: dict, receive, path_params: dict[str, str] | None = None) -> None:
        self._scope = scope
        self._receive = receive
        self._params: dict[str, str] = path_params or scope.get("route_params", {})
        self._headers_cache: dict[str, str] | None = None
        self._query_cache: dict[str, list[str]] | None = None
        self._cookies_cache: dict[str, str] | None = None
        self._multipart_cache: tuple[dict[str, list[str]], dict[str, list[UploadedFile]]] | None = None

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
        """dict[str, str]: Path parameters captured by the route pattern.

        Empty when the matched route has no parameters.
        """
        return self._params

    @property
    def method(self) -> str:
        """str: HTTP method of the request, e.g. ``"GET"``."""
        return self._rust.method

    @property
    def path(self) -> str:
        """str: Request path with the query string stripped."""
        return self._rust.path

    @property
    def headers(self) -> dict[str, str]:
        """dict[str, str]: Request headers with lower-cased keys.

        Parsed lazily on first access and cached for the request's lifetime.
        """
        if self._headers_cache is None:
            self._headers_cache = self._rust.headers
        return self._headers_cache

    def get_header(self, name: str) -> str | None:
        """Look up a single header without parsing the whole header block.

        Args:
            name: Header name; matching is case-insensitive.

        Returns:
            str | None: The header value, or ``None`` when the header is absent.
        """
        return self._rust.get_header(name)

    @property
    def cookies(self) -> dict[str, str]:
        """dict[str, str]: Cookies from the ``Cookie`` header, parsed lazily and cached.

        Values are unquoted when they arrive ``"quoted"``; whitespace around
        ``name=value`` pairs is ignored.
        """
        if self._cookies_cache is None:
            self._cookies_cache = _parse_cookies(self.get_header("cookie"))
        return self._cookies_cache

    def get_cookie(self, name: str, default: str | None = None) -> str | None:
        """Return a single cookie value, or ``default`` when it is absent.

        Args:
            name: Cookie name (exact match).
            default: Value returned when the cookie is not present.
        """
        return self.cookies.get(name, default)

    @property
    def query(self) -> dict[str, list[str]]:
        """dict[str, list[str]]: Parsed query string, parsed lazily and cached.

        Every value is a list, even for a single occurrence, so repeated keys
        such as ``?a=1&a=2`` are preserved as ``{"a": ["1", "2"]}``. The Rust
        getter re-parses on every call, so the result is memoised here.
        """
        if self._query_cache is None:
            self._query_cache = self._rust.query
        return self._query_cache

    async def body(self) -> bytes:
        """Read and return the complete request body.

        Streams the ASGI ``http.request`` chunks and concatenates them. When the
        scope carries ``ferronit.max_body_size`` (set by the application), reading
        past that limit raises :class:`BodyTooLarge` mid-stream.

        Returns:
            bytes: The full request body.

        Raises:
            BodyTooLarge: If the accumulated body exceeds the configured limit.
        """
        chunks: list[bytes] = []
        total = 0
        max_size = self._scope.get("ferronit.max_body_size")
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
        """Read the body and parse it as JSON.

        Returns:
            object: The decoded JSON value (any JSON type).

        Raises:
            RequestError: If the body is not valid JSON — mapped to HTTP 400.
        """
        await self.body()
        try:
            return self._rust.json()
        except ValueError as e:
            raise RequestError(f"Invalid JSON: {e}") from None

    async def model(self, target: type):
        """Parse the JSON body into a ``msgspec.Struct``, Pydantic model or dataclass.

        ``msgspec.Struct`` is decoded straight from the body bytes in one C pass
        (fast path); anything else goes through the registered codec (pydantic)
        or the dataclass fallback.

        Args:
            target: The model type to decode into.

        Returns:
            The decoded ``target`` instance.

        Raises:
            RequestError: If the body is malformed JSON, fails validation, or is
                not a JSON object/array — mapped to HTTP 400.
        """
        # Fast path: msgspec.Struct — декод из байт одним C-проходом.
        if isinstance(target, type) and hasattr(target, "__struct_fields__"):
            import msgspec

            body = await self.body()
            try:
                return msgspec.json.decode(body, type=target)
            except ValueError as exc:
                raise RequestError(f"Invalid JSON: {exc}") from None

        data = await self.json()
        if not isinstance(data, (dict, list)):
            raise RequestError("JSON body must be an object or array")
        return decode_json(data, target)

    async def form(self) -> dict[str, list[str]]:
        """Parse a URL-encoded or multipart form body into field value lists.

        For ``application/x-www-form-urlencoded`` every field is decoded as
        UTF-8. For ``multipart/form-data`` only text fields are returned — use
        :meth:`files` for uploaded files. Every value is a list so repeated
        fields are preserved.

        Returns:
            dict[str, list[str]]: Field names to their values.

        Raises:
            RequestError: If the content type is not a form type — mapped to 400.
        """
        content_type = (self.get_header("content-type") or "").lower()
        if content_type.startswith("application/x-www-form-urlencoded"):
            from urllib.parse import parse_qs

            body = await self.body()
            return parse_qs(body.decode("utf-8"))
        if content_type.startswith("multipart/form-data"):
            fields, _ = await self._multipart()
            return fields
        raise UnsupportedMediaType(f"Unsupported content type for form(): {content_type or 'none'}")

    async def files(self) -> dict[str, list[UploadedFile]]:
        """Return uploaded files from a ``multipart/form-data`` body.

        Returns:
            dict[str, list[UploadedFile]]: Field name → uploaded files.

        Raises:
            RequestError: If the content type is not ``multipart/form-data``.
        """
        _, files = await self._multipart()
        return files

    async def _multipart(self) -> tuple[dict[str, list[str]], dict[str, list[UploadedFile]]]:
        """Parse and cache the multipart body once per request (разбор — в Rust)."""
        if self._multipart_cache is None:
            content_type = (self.get_header("content-type") or "").lower()
            if not content_type.startswith("multipart/form-data"):
                raise UnsupportedMediaType("multipart/form-data expected")
            boundary = _boundary_from(content_type)
            if boundary is None:
                raise RequestError("multipart/form-data без boundary")
            await self.body()  # кладёт байты тела в Rust-ядро
            raw_fields, raw_files = self._rust.parse_multipart(boundary.decode("latin-1"))
            files = {
                name: [
                    UploadedFile(filename=filename, content=content, content_type=content_type)
                    for filename, content_type, content in uploads
                ]
                for name, uploads in raw_files.items()
            }
            self._multipart_cache = (raw_fields, files)
        return self._multipart_cache
