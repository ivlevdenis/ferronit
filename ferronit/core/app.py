"""Ferronit app — Rust routing, Python handler execution."""

from __future__ import annotations

import inspect
import time
from contextlib import asynccontextmanager
from typing import Any

import msgspec

from ferronit._core import FerronitApp as RustApp
from ferronit.contrib.logging import LoggerProtocol, StdlibLogger
from ferronit.contrib.tracing import current_trace_id
from ferronit.core.request import Request as PyRequest
from ferronit.core.request import RequestError
from ferronit.core.response import (
    JSONResponse,
    Response,
    StreamingResponse,
    TextResponse,
    _clean_header,
    _json_encode,
)
from ferronit.di import Container
from ferronit.errors import InternalError, NotFoundError
from ferronit.middleware import Middleware
from ferronit.openapi import OpenAPI

__all__ = ["Ferronit"]


class Ferronit:
    """ASGI application: Rust routing, Python handlers.

    Register HTTP routes with :meth:`route`, WebSocket endpoints with
    :meth:`websocket`, and extensions with :meth:`use` / :meth:`mount`. An
    instance is itself the ASGI callable, so pass it directly to a server.

    Args:
        debug: When ``True``, re-raise handler exceptions instead of turning
            them into a 500 response.
        access_log: When ``True``, log every request (method, path, status,
            duration) through the injected ``logger``.
        logger: The logger the framework writes to (see
            :class:`ferronit.contrib.logging.LoggerProtocol`). Defaults to a
            :class:`~ferronit.contrib.logging.StdlibLogger` bound to the
            ``"ferronit"`` standard-library logger.
        max_body_size: Maximum accepted request body in bytes; larger bodies
            yield HTTP 413. ``None`` disables the limit.

    Attributes:
        openapi: The :class:`~ferronit.openapi.OpenAPI` schema builder that every
            registered route feeds.
    """

    __slots__ = (
        "_access_log",
        "_app",
        "_container",
        "_debug",
        "_deferred_routes",
        "_lifespan_fn",
        "_logger",
        "_max_body_size",
        "_middleware",
        "_mw_cache",
        "_openapi",
        "_rsgi_lifespan_cm",
        "_ws_origins",
    )

    def __init__(
        self,
        *,
        debug: bool = False,
        access_log: bool = False,
        logger: LoggerProtocol | None = None,
        max_body_size: int | None = None,
        container: Container | None = None,
        lifespan=None,
    ) -> None:
        self._app = RustApp()
        self._openapi = OpenAPI()
        self._debug = debug
        self._access_log = access_log
        self._logger = logger if logger is not None else StdlibLogger("ferronit")
        self._middleware = Middleware()
        self._mw_cache: dict = {}
        self._ws_origins: dict[str, list[str]] = {}
        self._max_body_size = max_body_size
        self._container = container if container is not None else Container()
        self._lifespan_fn = lifespan
        self._rsgi_lifespan_cm: Any = None
        self._deferred_routes: list = []

    def _log_access(self, method: str, path: str, status: int, start_ns: int) -> None:
        """Log one access line (method, path, status, duration) when enabled.

        The duration is measured from ``start_ns`` (a ``time.perf_counter_ns()``
        taken before the handler ran). Structured fields (``event``, ``method``,
        ``path``, ``status``, ``duration_ms``) become top-level JSON fields.

        Args:
            method: HTTP method.
            path: Request path (without query string).
            status: Response status code.
            start_ns: ``time.perf_counter_ns()`` taken before handler execution.
        """
        if not self._access_log:
            return
        duration_ms = (time.perf_counter_ns() - start_ns) / 1_000_000
        self._logger.info(
            f"{method} {path} {status} {duration_ms:.3f}ms",
            event="access",
            method=method,
            path=path,
            status=status,
            duration_ms=round(duration_ms, 3),
        )

    @property
    def openapi(self) -> OpenAPI:
        """OpenAPI: The schema builder fed by every registered route."""
        return self._openapi

    @property
    def container(self) -> Container:
        """Container: The DI registry shared by handlers and services."""
        return self._container

    def lifespan(self, fn):
        """Register the single async lifespan function.

        The function is an async generator: setup runs before ``yield`` (before
        the first request), teardown after it (on shutdown). Resources created
        in setup are typically stored via ``app.container.singleton(...)``.

        Args:
            fn: Async generator function ``setup → yield → teardown``, or an
                ``@asynccontextmanager``-wrapped function.

        Returns:
            The function unchanged, so it can be used as a decorator.
        """
        self._lifespan_fn = fn
        return fn

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
            # Auto-bind: request-параметры резолвим сразу, DI — на старте (pre-bind)
            from ferronit.binding import build, plan

            p = plan(handler, container=self._container)
            if p.di_keys:
                self._deferred_routes.append((_methods, path, handler, p))
            else:
                wrapped = build(handler, p, container=self._container)
                for m in _methods:
                    self._app.add_route(m, path, wrapped)

            for m in _methods:
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

    def _flush_deferred(self) -> None:
        """Resolve DI deps for deferred routes and register them in the router.

        Called after lifespan startup (and lazily before the first HTTP request
        when no lifespan runs), so DI params are pre-bound once rather than
        resolved on every request.
        """
        if not self._deferred_routes:
            return
        from ferronit.binding import build

        deferred, self._deferred_routes = self._deferred_routes, []
        for methods, path, handler, p in deferred:
            resolved = {name: self._container.get(key) for name, key in p.di_keys}
            wrapped = build(handler, p, container=self._container, resolved=resolved)
            for m in methods:
                self._app.add_route(m, path, wrapped)

    async def _run_lifespan(self, scope: dict, receive, send) -> None:
        """Serve ASGI lifespan, running the registered lifespan function around it.

        Startup runs before ``lifespan.startup.complete``; teardown runs before
        ``lifespan.shutdown.complete``. Without a registered lifespan the
        startup/shutdown messages complete immediately.
        """
        fn = self._lifespan_fn
        if fn is None:
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        cm = asynccontextmanager(fn) if inspect.isasyncgenfunction(fn) else fn
        async with cm():
            self._flush_deferred()
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return

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
            await self._run_lifespan(scope, receive, send)
            return
        if scope["type"] == "websocket":
            from ferronit.websocket import WebSocket
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

        self._flush_deferred()

        method = scope.get("method", "GET")
        path = scope.get("path", "/")
        start_ns = time.perf_counter_ns()

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
                self._log_access(method, path, 204, start_ns)
                return

        # тело не читается здесь — только прокидываем лимит в Request
        if self._max_body_size is not None:
            scope["ferronit.max_body_size"] = self._max_body_size

        result = self._app.resolve(method, path)
        if result is None:
            self._log_access(method, path, 404, start_ns)
            await _send_error(send, NotFoundError("Route not found"))
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
        except RequestError as exc:
            self._log_access(method, path, exc.status, start_ns)
            await _send_error(send, exc)
            return
        except Exception as exc:
            if self._debug:
                raise
            self._logger.error("Unhandled exception", exc_info=exc, event="error")
            self._log_access(method, path, 500, start_ns)
            await _send_error(send, InternalError("Internal server error"))
            return

        resp = _to_response(py_result)
        self._log_access(method, path, resp._status, start_ns)
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

    async def __rsgi__(self, scope, proto) -> None:
        """RSGI entry point (granian) — same dispatch, native transport.

        Mirrors :meth:`__call__` for HTTP: the route is resolved in Rust, the
        handler runs through the middleware chain, and its result is normalised
        by :func:`_to_response`. The difference is transport — the request comes
        from a native RSGI ``scope`` and the response goes out with one
        ``proto.response_bytes(...)`` call instead of two ASGI ``send`` events.

        Args:
            scope: Native RSGI scope (``method`` / ``path`` / ``query_string`` /
                ``headers`` attributes).
            proto: Native RSGI protocol (``response_*`` methods; ``await proto()``
                yields the request body).
        """
        self._flush_deferred()

        method = scope.method
        path = scope.path
        start_ns = time.perf_counter_ns()

        # Rust CORS preflight — только для разрешённого Origin
        if method == "OPTIONS" and self._app.has_cors():
            origin = _rsgi_header(scope, "origin") or ""
            preflight = self._app.cors_preflight_headers(origin)
            if preflight is not None:
                proto.response_bytes(204, list(preflight.items()), b"")
                self._log_access(method, path, 204, start_ns)
                return

        result = self._app.resolve(method, path)
        if result is None:
            self._log_access(method, path, 404, start_ns)
            _rsgi_error(proto, NotFoundError("Route not found"))
            return

        handler_py, params = result
        req = _rsgi_request(scope, proto, params, self._max_body_size)

        try:
            wrapped = self._mw_cache.get(handler_py)
            if wrapped is None:
                wrapped = self._middleware.wrap(handler_py)
                self._mw_cache[handler_py] = wrapped
            py_result = wrapped(req)
            if hasattr(py_result, "__await__"):
                py_result = await py_result
        except RequestError as exc:
            self._log_access(method, path, exc.status, start_ns)
            _rsgi_error(proto, exc)
            return
        except Exception as exc:
            if self._debug:
                raise
            self._logger.error("Unhandled exception", exc_info=exc, event="error")
            self._log_access(method, path, 500, start_ns)
            _rsgi_error(proto, InternalError("Internal server error"))
            return

        resp = _to_response(py_result)
        self._log_access(method, path, resp._status, start_ns)

        # Rust CORS origin header — только для разрешённого Origin
        if self._app.has_cors():
            origin = _rsgi_header(scope, "origin") or ""
            allowed = self._app.cors_origin(origin)
            if allowed:
                resp._headers["Access-Control-Allow-Origin"] = allowed

        # Trace ID propagation
        tid = current_trace_id()
        if tid:
            resp._headers["X-Trace-Id"] = tid

        # Rust GZip — уважаем q-факторы
        accept_enc = req.get_header("accept-encoding") or ""
        if _wants_gzip(accept_enc) and resp._body:
            body_bytes = resp._body if isinstance(resp._body, bytes) else resp._body.encode()
            resp._body = bytes(self._app.gzip_compress(body_bytes))
            resp._headers["Content-Encoding"] = "gzip"

        _rsgi_send(proto, resp)

    def __rsgi_init__(self, loop) -> None:
        """RSGI worker startup (granian) — run lifespan setup, then flush DI.

        RSGI has no ASGI lifespan channel; granian instead calls ``__rsgi_init__``
        once per worker before serving requests. We run the registered lifespan
        up to its ``yield`` (pool creation and DI registration), then resolve any
        deferred DI routes.
        """
        fn = self._lifespan_fn
        if fn is None:
            self._flush_deferred()
            return
        cm = asynccontextmanager(fn) if inspect.isasyncgenfunction(fn) else fn
        self._rsgi_lifespan_cm = cm()
        loop.run_until_complete(self._rsgi_lifespan_cm.__aenter__())
        self._flush_deferred()

    def __rsgi_del__(self, loop) -> None:
        """RSGI worker shutdown (granian) — run lifespan teardown."""
        cm = self._rsgi_lifespan_cm
        if cm is not None:
            loop.run_until_complete(cm.__aexit__(None, None, None))
            self._rsgi_lifespan_cm = None


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


def _is_struct(obj) -> bool:
    """True for a msgspec Struct (it carries ``__struct_fields__``)."""
    return hasattr(obj, "__struct_fields__")


def _struct_payload(result) -> bool:
    """True for a msgspec Struct, or a non-empty list of them."""
    return _is_struct(result) or (
        isinstance(result, list) and bool(result) and _is_struct(result[0])
    )


def _to_response(result) -> Response:
    """Normalise a handler return value into a :class:`Response`.

    Recognised types, in order: a ``msgspec.Struct`` or a list of them (JSON 200
    via msgspec's C encoder); ``dict`` or ``list`` (JSON 200 via msgspec's C
    encoder, Rust fallback for unsupported types); a
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
    if _struct_payload(result):
        return Response(
            body=_json_encode(result),
            status=200,
            content_type="application/json; charset=utf-8",
        )
    if isinstance(result, (dict, list)):
        return Response(
            body=_json_encode(result),
            status=200,
            content_type="application/json; charset=utf-8",
        )
    # (payload, status) — документированный паттерн: `return {"error": "..."}, 404`
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], int):
        payload, tuple_status = result
        if _struct_payload(payload) or isinstance(payload, (dict, list)):
            return Response(
                body=_json_encode(payload),
                status=tuple_status,
                content_type="application/json; charset=utf-8",
            )
        if isinstance(payload, str):
            return Response(body=payload.encode(), status=tuple_status, content_type="text/plain")
        inner = _to_response(payload)
        inner._status = tuple_status
        return inner
    if isinstance(result, Response):
        return result  # already has headers
    if isinstance(result, str):
        return TextResponse(result)
    if hasattr(result, "model_dump"):
        return JSONResponse.from_model(result)
    if hasattr(result, "__dataclass_fields__") or hasattr(result, "__columns__") or hasattr(result, "__struct_fields__"):
        return Response(
            body=_json_encode(result),
            status=200,
            content_type="application/json; charset=utf-8",
        )
    if hasattr(result, "__aiter__"):
        return StreamingResponse(result)
    return TextResponse(str(result))


async def _send_error(send, exc: RequestError) -> None:
    """Send a structured JSON error response.

    The body is the same for every failure::

        {"code": "validation_error", "message": "...", "errors": [...]}

    so a client parses one contract for 4xx and 5xx alike.

    Args:
        send: ASGI send channel.
        exc: The error to render — its ``status``, ``code`` and ``errors`` are used.
    """
    body = msgspec.json.encode(exc.payload())
    await send({
        "type": "http.response.start",
        "status": exc.status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


def _rsgi_request(scope, proto, params, max_body_size):
    """Build a :class:`Request` from a native RSGI scope.

    The native scope is mapped onto the same shape :class:`Request` expects
    from ASGI (method / path / query_string / headers), and ``receive`` reads
    the body once via ``await proto()``.

    Args:
        scope: Native RSGI scope.
        proto: Native RSGI protocol.
        params: Path parameters captured by the route.
        max_body_size: Body limit propagated into the Request.

    Returns:
        Request: The request, ready for the middleware chain.
    """
    try:
        raw_headers = list(scope.headers.items())
    except Exception:
        raw_headers = []

    qs = getattr(scope, "query_string", "") or ""
    asgi_scope = {
        "method": getattr(scope, "method", "GET"),
        "path": getattr(scope, "path", "/"),
        "query_string": qs,
        "headers": raw_headers,
    }
    if max_body_size is not None:
        asgi_scope["ferronit.max_body_size"] = max_body_size

    body_cache = None

    async def receive():
        nonlocal body_cache
        if body_cache is None:
            body_cache = await proto()
        return {"type": "http.request", "body": body_cache, "more_body": False}

    return PyRequest(asgi_scope, receive, params)


def _rsgi_header(scope, name: str) -> str | None:
    """Read a header from a native RSGI scope, case-insensitively."""
    needle = name.lower()
    try:
        for k, v in scope.headers.items():
            if str(k).lower() == needle:
                return v if isinstance(v, str) else v.decode("latin-1")
    except Exception:
        pass
    return None


def _rsgi_error(proto, exc: RequestError) -> None:
    """Write a structured JSON error response through the RSGI protocol."""
    body = msgspec.json.encode(exc.payload())
    proto.response_bytes(exc.status, [("content-type", "application/json")], body)


def _rsgi_send(proto, resp: Response) -> None:
    """Send a :class:`Response` through the RSGI protocol (one native call)."""
    headers = [("content-type", _clean_header(resp._content_type))]
    for k, v in resp._headers.items():
        headers.append((_clean_header(k), _clean_header(v)))
    body = resp._body if isinstance(resp._body, bytes) else resp._body.encode()
    proto.response_bytes(resp._status, headers, body)
