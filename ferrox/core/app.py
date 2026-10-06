"""Ferrox app — Rust routing, Python handler execution."""

from __future__ import annotations

from ferrox._core import FerroxApp as RustApp
from ferrox._core import Response as RustResp
from ferrox.contrib.tracing import current_trace_id
from ferrox.core.request import BodyTooLarge, PathParamError, RequestError
from ferrox.core.request import Request as PyRequest
from ferrox.core.response import JSONResponse, Response, StreamingResponse, TextResponse
from ferrox.middleware import Middleware
from ferrox.openapi import OpenAPI

__all__ = ["Ferrox"]


class Ferrox:
    """ASGI application: Rust routing, Python handlers.

    Register HTTP routes with :meth:`route`, WebSocket endpoints with
    :meth:`websocket`, and extensions with :meth:`use` / :meth:`mount`. An
    instance is itself the ASGI callable, so pass it directly to a server.

    Args:
        debug: When ``True``, re-raise handler exceptions instead of turning
            them into a 500 response.
        max_body_size: Maximum accepted request body in bytes; larger bodies
            yield HTTP 413. ``None`` disables the limit.

    Attributes:
        openapi: The :class:`~ferrox.openapi.OpenAPI` schema builder that every
            registered route feeds.
    """

    __slots__ = ("_app", "_debug", "_max_body_size", "_middleware", "_mw_cache", "_openapi", "_ws_origins")

    def __init__(self, *, debug: bool = False, max_body_size: int | None = None) -> None:
        self._app = RustApp()
        self._openapi = OpenAPI()
        self._debug = debug
        self._middleware = Middleware()
        self._mw_cache: dict = {}
        self._ws_origins: dict[str, list[str]] = {}
        self._max_body_size = max_body_size

    @property
    def openapi(self) -> OpenAPI:
        """OpenAPI: The schema builder fed by every registered route."""
        return self._openapi

    def use(self, mw):
        """Register middleware or a CORS-style extension.

        If ``mw`` exposes an ``apply(app)`` method it is applied immediately
        (the CORS helper uses this); otherwise it is appended to the middleware
        chain. The compiled middleware cache is reset either way.

        Args:
            mw: A middleware object, or anything exposing ``apply(app)``.
        """
        if hasattr(mw, "apply"):
            mw.apply(self)
        else:
            self._middleware.add(mw)
        self._mw_cache.clear()

    def route(self, path: str, methods: list[str] | None = None, **meta):
        """Decorator registering an HTTP handler for one or more methods.

        The handler is wrapped for dependency injection and registered both in
        the router and in the OpenAPI document. Extra keyword arguments are
        forwarded to :meth:`OpenAPI.add_route` (for example ``summary=`` or
        ``tags=``).

        Args:
            path: Route pattern, e.g. ``"/users/{id}"``.
            methods: HTTP methods to serve; defaults to ``["GET"]``.
            **meta: OpenAPI metadata forwarded to the schema builder.

        Returns:
            Callable: A decorator that returns the original handler unchanged.
        """
        _methods = [m.upper() for m in (methods or ["GET"])]

        def decorator(handler):
            # Auto-inject if handler has type hints beyond just `req`
            from ferrox.injection import inject as _inject
            wrapped = _inject(handler)

            for m in _methods:
                self._app.add_route(m, path, wrapped)
                self._openapi.add_route(m, path, handler, **meta)
            return handler

        return decorator

    def websocket(self, path: str, *, origins: list[str] | None = None):
        """Decorator registering a WebSocket handler.

        Args:
            path: WebSocket path, e.g. ``"/ws"``.
            origins: Allowed ``Origin`` header values. When provided, a
                connection whose Origin is not listed is rejected with close
                code 1008 before the handler runs.

        Returns:
            Callable: A decorator that returns the original handler unchanged.
        """
        if origins:
            self._ws_origins[path] = origins

        def decorator(handler):
            self._app.add_route("WS", path, handler)
            return handler
        return decorator

    def mount(self, prefix: str, static):
        """Mount static files or a sub-application at a URL prefix.

        Args:
            prefix: URL prefix served by ``static``.
            static: Object providing a ``mount(app, prefix)`` method.
        """
        static.mount(self, prefix)

    async def __call__(self, scope: dict, receive, send) -> None:
        """ASGI entry point — dispatch one lifespan, WebSocket, or HTTP event.

        For HTTP the route is resolved in Rust, the handler runs through the
        middleware chain, and its result is normalised by :func:`_to_response`.
        The ``Access-Control-Allow-Origin`` header (when CORS is enabled) and
        the ``X-Trace-Id`` header are added *after* conversion, so they survive
        dict/Pydantic returns; the body is then optionally gzipped and sent.
        ``PathParamError``, ``RequestError`` and ``BodyTooLarge`` are mapped to
        HTTP 404, 400 and 413 respectively.

        Args:
            scope: The ASGI connection scope.
            receive: ASGI receive channel.
            send: ASGI send channel.
        """
        if scope["type"] == "lifespan":
            await _lifespan(scope, receive, send)
            return
        if scope["type"] == "websocket":
            from ferrox.websocket import WebSocket
            path = scope.get("path", "/")
            # WS CSRF guard: проверка Origin для защищённых эндпоинтов
            allowed_origins = self._ws_origins.get(path)
            if allowed_origins is not None:
                origin = _scope_header(scope, "origin") or ""
                if origin not in allowed_origins:
                    await send({"type": "websocket.close", "code": 1008})
                    return
            result = self._app.resolve("WS", path)
            if result is None:
                await send({"type": "websocket.close", "code": 404})
                return
            handler, _ = result
            ws = WebSocket(scope, receive, send)
            try:
                r = handler(ws)
                if hasattr(r, "__await__"):
                    await r
            except Exception:
                if ws.state.value < 2:
                    await ws.close(1011)
            return

        method = scope.get("method", "GET")
        path = scope.get("path", "/")

        # Rust CORS preflight — только для разрешённого Origin
        if method == "OPTIONS" and self._app.has_cors():
            origin = _scope_header(scope, "origin") or ""
            preflight = self._app.cors_preflight_headers(origin)
            if preflight is not None:
                await send({
                    "type": "http.response.start",
                    "status": 204,
                    "headers": [(k.encode(), v.encode()) for k, v in preflight.items()],
                })
                await send({"type": "http.response.body", "body": b""})
                return

        # тело не читается здесь — только прокидываем лимит в Request
        if self._max_body_size is not None:
            scope["ferrox.max_body_size"] = self._max_body_size

        result = self._app.resolve(method, path)
        if result is None:
            await _send_empty(send, 404)
            return

        handler_py, params = result
        req = PyRequest(scope, receive, params)

        try:
            wrapped = self._mw_cache.get(handler_py)
            if wrapped is None:
                wrapped = self._middleware.wrap(handler_py)
                self._mw_cache[handler_py] = wrapped
            py_result = wrapped(req)
            if hasattr(py_result, "__await__"):
                py_result = await py_result
        except PathParamError:
            await _send_empty(send, 404)
            return
        except RequestError:
            await _send_empty(send, 400)
            return
        except BodyTooLarge:
            await _send_empty(send, 413)
            return
        except Exception:
            if self._debug:
                raise
            await _send_empty(send, 500)
            return

        resp = _to_response(py_result)
        # Rust CORS origin header — только для разрешённого Origin
        if self._app.has_cors():
            origin = _scope_header(scope, "origin") or ""
            allowed = self._app.cors_origin(origin)
            if allowed:
                resp._headers["Access-Control-Allow-Origin"] = allowed

        # Trace ID propagation
        tid = current_trace_id()
        if tid:
            resp._headers["X-Trace-Id"] = tid

        # Rust GZip — уважаем q-факторы: gzip;q=0 означает "не сжимать"
        accept_enc = req.get_header("accept-encoding") or ""
        if _wants_gzip(accept_enc) and resp._body:
            body_bytes = resp._body if isinstance(resp._body, bytes) else resp._body.encode()
            compressed = self._app.gzip_compress(body_bytes)
            resp._body = bytes(compressed)
            resp._headers["Content-Encoding"] = "gzip"

        await resp._send(send)


def _wants_gzip(accept_encoding: str) -> bool:
    """Return whether the client accepts gzip, honouring q-factors.

    A ``gzip;q=0`` entry means "do not compress".

    Args:
        accept_encoding: Raw ``Accept-Encoding`` header value.

    Returns:
        bool: ``True`` when gzip has a positive q-factor.
    """
    for part in accept_encoding.split(","):
        name, _, params = part.partition(";")
        if name.strip().lower() != "gzip":
            continue
        q = 1.0
        for p in params.split(";"):
            p = p.strip()
            if p.lower().startswith("q="):
                try:
                    q = float(p[2:])
                except ValueError:
                    q = 0.0
        return q > 0
    return False


def _scope_header(scope: dict, name: str) -> str | None:
    """Find a header in the raw ASGI scope before a Request is built.

    Args:
        scope: The ASGI connection scope.
        name: Header name; matching is case-insensitive.

    Returns:
        str | None: The decoded header value, or ``None`` when absent.
    """
    needle = name.lower().encode("latin-1")
    for k, v in scope.get("headers", []):
        if k.lower() == needle:
            return v.decode("latin-1")
    return None


def _to_response(result) -> Response:
    """Normalise a handler return value into a :class:`Response`.

    Recognised types, in order: ``dict`` or ``list`` (JSON 200); a
    ``(payload, status)`` tuple whose second item is an int; an existing
    :class:`Response`; ``str``
    (text/plain); any object with ``model_dump`` or ``__dataclass_fields__``
    (serialised as JSON); and any async iterator (streamed). Anything else is
    sent as text via ``str(result)``.

    Args:
        result: The raw value returned by the route handler.

    Returns:
        Response: The response object ready to be sent.
    """
    if isinstance(result, (dict, list)):
        r = RustResp.json(result, 200)
        return Response(body=bytes(r.body), status=r.status, content_type=r.content_type)
    # (payload, status) — документированный паттерн: `return {"error": "..."}, 404`
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], int):
        payload, tuple_status = result
        if isinstance(payload, (dict, list)):
            r = RustResp.json(payload, tuple_status)
            return Response(body=bytes(r.body), status=r.status, content_type=r.content_type)
        if isinstance(payload, str):
            r = RustResp.text(payload, tuple_status)
            return Response(body=bytes(r.body), status=r.status, content_type=r.content_type)
        inner = _to_response(payload)
        inner._status = tuple_status
        return inner
    if isinstance(result, Response):
        return result  # already has headers
    if isinstance(result, str):
        return TextResponse(result)
    if hasattr(result, "model_dump"):
        return JSONResponse.from_model(result)
    if hasattr(result, "__dataclass_fields__"):
        r = RustResp.json(result, 200)
        return Response(body=bytes(r.body), status=r.status, content_type=r.content_type)
    if hasattr(result, "__aiter__"):
        return StreamingResponse(result)
    return TextResponse(str(result))


async def _lifespan(scope: dict, receive, send) -> None:
    """Answer ASGI lifespan startup/shutdown messages until shutdown.

    Args:
        scope: The ASGI lifespan scope.
        receive: ASGI receive channel.
        send: ASGI send channel.
    """
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


async def _send_empty(send, status: int) -> None:
    """Send an empty ``text/plain`` response with the given status.

    Args:
        send: ASGI send channel.
        status: HTTP status code for the response.
    """
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"text/plain")],
    })
    await send({"type": "http.response.body", "body": b""})
