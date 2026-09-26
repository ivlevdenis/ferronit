"""Velox app — Rust routing, Python handler execution."""

from __future__ import annotations

from velox.contrib.tracing import current_trace_id
from velox.core.request import BodyTooLarge, Request as PyRequest, RequestError
from velox.core.response import JSONResponse, Response, StreamingResponse, TextResponse
from velox.middleware import Middleware
from velox.openapi import OpenAPI
from velox_core import Response as RustResp, VeloxApp as RustApp

__all__ = ["Velox"]


class Velox:
    """ASGI — Rust routing (fast), Python handlers (async/streaming)."""

    __slots__ = ("_app", "_openapi", "_debug", "_middleware", "_mw_cache", "_ws_origins", "_max_body_size")

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
        return self._openapi

    def use(self, mw):
        """Add middleware or CORS config."""
        if hasattr(mw, "apply"):
            mw.apply(self)
        else:
            self._middleware.add(mw)
        self._mw_cache.clear()

    def route(self, path: str, methods: list[str] | None = None, **meta):
        _methods = [m.upper() for m in (methods or ["GET"])]

        def decorator(handler):
            # Auto-inject if handler has type hints beyond just `req`
            from velox.injection import inject as _inject
            wrapped = _inject(handler)

            for m in _methods:
                self._app.add_route(m, path, wrapped)
                self._openapi.add_route(m, path, handler, **meta)
            return handler

        return decorator

    def websocket(self, path: str, *, origins: list[str] | None = None):
        if origins:
            self._ws_origins[path] = origins

        def decorator(handler):
            self._app.add_route("WS", path, handler)
            return handler
        return decorator

    def mount(self, prefix: str, static):
        """Mount static files or sub-app at a prefix."""
        static.mount(self, prefix)

    async def __call__(self, scope: dict, receive, send) -> None:
        if scope["type"] == "lifespan":
            await _lifespan(scope, receive, send)
            return
        if scope["type"] == "websocket":
            from velox.websocket import WebSocket
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

        body = b""

        # тело не читается здесь — только прокидываем лимит в Request
        if self._max_body_size is not None:
            scope["velox.max_body_size"] = self._max_body_size

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
    """Парсинг Accept-Encoding с учётом q-факторов (gzip;q=0 → False)."""
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
    """Поиск заголовка в scope до создания Request (для CORS)."""
    needle = name.lower().encode("latin-1")
    for k, v in scope.get("headers", []):
        if k.lower() == needle:
            return v.decode("latin-1")
    return None


def _to_response(result) -> Response:
    if isinstance(result, dict):
        r = RustResp.json(result, 200)
        return Response(body=bytes(r.body), status=r.status, content_type=r.content_type)
    if isinstance(result, Response):
        return result  # already has headers
    if isinstance(result, str):
        return TextResponse(result)
    if hasattr(result, "model_dump"):
        return JSONResponse.from_model(result)
    if hasattr(result, "__dataclass_fields__"):
        return JSONResponse.from_model(result)
    if hasattr(result, "__aiter__"):
        return StreamingResponse(result)
    return TextResponse(str(result))


async def _lifespan(scope: dict, receive, send) -> None:
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


async def _send_empty(send, status: int) -> None:
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"text/plain")],
    })
    await send({"type": "http.response.body", "body": b""})
