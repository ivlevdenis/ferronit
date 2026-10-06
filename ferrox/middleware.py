"""Middleware stack — pre/post request hooks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ferrox.core.request import Request
from ferrox.core.response import Response

__all__ = ["Middleware", "MiddlewareFunc"]

Handler = Callable[[Request], Response | Awaitable[Response]]
MiddlewareFunc = Callable[[Request, Handler], Response | Awaitable[Response]]


class Middleware:
    """Stack of middleware functions."""

    __slots__ = ("_stack",)

    def __init__(self) -> None:
        self._stack: list[MiddlewareFunc] = []

    def add(self, mw: MiddlewareFunc) -> None:
        """Append a middleware function to the stack.

        Args:
            mw: Middleware callable ``(request, next_handler) -> response``;
                may be sync or async.
        """
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
