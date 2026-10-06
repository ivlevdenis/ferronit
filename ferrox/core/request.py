"""Request object — Rust-native parsing for headers, query, JSON."""

from __future__ import annotations

from ferrox._core import Request as RustRequest
from ferrox.contrib.pydantic import decode_json

__all__ = ["BodyTooLarge", "PathParamError", "Request", "RequestError"]


class RequestError(Exception):
    """Invalid client body or JSON → HTTP 400.

    Raised by :meth:`Request.json` (malformed JSON) and :meth:`Request.model`
    (payload that is not an object or array). The application maps it to a 400
    response.
    """


class BodyTooLarge(Exception):
    """Request body exceeds ``max_body_size`` → HTTP 413.

    Raised by :meth:`Request.body` while streaming chunks, as soon as the
    accumulated size passes ``ferrox.max_body_size`` taken from the ASGI scope.
    The application maps it to a 413 response.
    """


class PathParamError(Exception):
    """A path parameter failed type coercion → HTTP 404 (ASVS 2.1.1).

    The application maps it to a 404 so a malformed parameter never reveals
    that the route exists.
    """


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
    """

    __slots__ = ("_headers_cache", "_params", "_receive", "_rust", "_scope")

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
    def query(self) -> dict[str, list[str]]:
        """dict[str, list[str]]: Parsed query string, parsed lazily.

        Every value is a list, even for a single occurrence, so repeated keys
        such as ``?a=1&a=2`` are preserved as ``{"a": ["1", "2"]}``.
        """
        return self._rust.query

    async def body(self) -> bytes:
        """Read and return the complete request body.

        Streams the ASGI ``http.request`` chunks and concatenates them. When the
        scope carries ``ferrox.max_body_size`` (set by the application), reading
        past that limit raises :class:`BodyTooLarge` mid-stream.

        Returns:
            bytes: The full request body.

        Raises:
            BodyTooLarge: If the accumulated body exceeds the configured limit.
        """
        chunks: list[bytes] = []
        total = 0
        max_size = self._scope.get("ferrox.max_body_size")
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
        """Parse the JSON body into a Pydantic model or dataclass.

        Args:
            target: The model type to decode into.

        Returns:
            The decoded ``target`` instance.

        Raises:
            RequestError: If the body is malformed JSON or is not a JSON object
                or array — mapped to HTTP 400.
        """
        data = await self.json()
        if not isinstance(data, (dict, list)):
            raise RequestError("JSON body must be an object or array")
        return decode_json(data, target)

    async def form(self) -> dict[str, list[str]]:
        """Parse a URL-encoded form body into a mapping of value lists.

        Returns:
            dict[str, list[str]]: Field names to their values; every value is a
            list, so repeated fields are preserved.
        """
        from urllib.parse import parse_qs
        body = await self.body()
        return parse_qs(body.decode("latin-1"))
