"""Middleware stack — pre/post request hooks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ferronit.core.request import Request
from ferronit.core.response import Response

__all__ = ["Middleware", "MiddlewareFunc"]

Handler = Callable[[Request], Response | Awaitable[Response]]
MiddlewareFunc = Callable[[Request, Handler], Response | Awaitable[Response]]
AsyncHandler = Callable[[Request], Awaitable[Response]]


def _as_async(handler: Handler) -> AsyncHandler:
    """Wrap a sync-or-async handler into an always-awaitable callable."""

    async def call(req: Request) -> Response:
        result = handler(req)
        if isinstance(result, Response):
            return result
        return await result

    return call


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

    def wrap(self, handler: Handler) -> AsyncHandler:
        """Wrap handler with middleware stack.

        Returns:
            AsyncHandler: Always-awaitable callable, so the dispatcher can
            ``await`` the result without an awaitable check.
        """
        wrapped: AsyncHandler = _as_async(handler)
        for mw in reversed(self._stack):
            outer = mw  # capture mw

            def make(outer: MiddlewareFunc = outer, inner: AsyncHandler = wrapped):
                async def middleware_wrapper(req: Request) -> Response:
                    result = outer(req, inner)
                    if isinstance(result, Response):
                        return result
                    return await result

                return middleware_wrapper

            wrapped = make()
        return wrapped
