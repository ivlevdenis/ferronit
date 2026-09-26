"""Middleware stack — pre/post request hooks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from velox.core.request import Request
from velox.core.response import Response

__all__ = ["MiddlewareFunc", "Middleware"]

Handler = Callable[[Request], Response | Awaitable[Response]]
MiddlewareFunc = Callable[[Request, Handler], Response | Awaitable[Response]]


class Middleware:
    """Stack of middleware functions."""

    __slots__ = ("_stack",)

    def __init__(self) -> None:
        self._stack: list[MiddlewareFunc] = []

    def add(self, mw: MiddlewareFunc) -> None:
        self._stack.append(mw)

    def wrap(self, handler: Handler) -> Handler:
        """Wrap handler with middleware stack."""
        wrapped = handler
        for mw in reversed(self._stack):
            outer = mw  # capture mw

            def make(outer=outer, inner=wrapped):
                async def middleware_wrapper(req: Request):
                    result = outer(req, inner)
                    if hasattr(result, "__await__"):
                        result = await result
                    return result

                return middleware_wrapper

            wrapped = make()
        return wrapped
