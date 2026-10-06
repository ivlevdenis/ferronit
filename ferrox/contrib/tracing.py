"""Distributed tracing — X-Trace-Id header injection and propagation."""

from __future__ import annotations

import uuid
from contextvars import ContextVar

from ferrox.core.request import Request
from ferrox.core.response import Response
from ferrox.hexagonal import Logger, PrintLogger

__all__ = ["TraceLogger", "current_trace_id", "trace_middleware"]

# Context variable shared across all coroutines in a request
_trace_id: ContextVar[str] = ContextVar("trace_id", default="")

TRACE_HEADER = "X-Trace-Id"


def current_trace_id() -> str:
    """Get current trace ID from context."""
    return _trace_id.get()


async def trace_middleware(req: Request, next_handler) -> Response:
    """Middleware: extract or generate X-Trace-Id, propagate to context and response."""
    trace_id = req.headers.get(TRACE_HEADER.lower()) or str(uuid.uuid4())
    _trace_id.set(trace_id)

    result = next_handler(req)
    if hasattr(result, "__await__"):
        result = await result

    if isinstance(result, Response):
        result._headers[TRACE_HEADER] = trace_id

    return result


class TraceLogger(Logger):
    """Logger that prepends trace_id to every message."""

    def __init__(self, delegate: Logger | None = None):
        self._delegate = delegate or PrintLogger()

    def _fmt(self, msg: str) -> str:
        tid = _trace_id.get("-")
        return f"[{tid[:8] if len(tid) > 8 else tid}] {msg}"

    async def info(self, msg: str, **ctx) -> None:
        """Log an info message with the current trace ID prefixed."""
        await self._delegate.info(self._fmt(msg), **ctx)

    async def warning(self, msg: str, **ctx) -> None:
        """Log a warning message with the current trace ID prefixed."""
        await self._delegate.warning(self._fmt(msg), **ctx)

    async def error(self, msg: str, **ctx) -> None:
        """Log an error message with the current trace ID prefixed."""
        await self._delegate.error(self._fmt(msg), **ctx)
