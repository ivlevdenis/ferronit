"""Auto-injection — zero-overhead: pre-computed at registration time."""

from __future__ import annotations

import inspect
from typing import Any, get_type_hints

from velox.core.request import PathParamError, Request, RequestError

__all__ = ["inject"]


def inject(handler):
    """Wrap handler with pre-computed parameter resolution strategy.

    Three fast paths (chosen at registration time, zero per-request overhead):
        1. pass_through — handler expects Request directly
        2. inject_params — handler has typed path/query params
        3. default — fallback
    """
    sig = inspect.signature(handler)
    try:
        hints = get_type_hints(handler)
    except Exception:
        hints = {}
    is_async = inspect.iscoroutinefunction(handler)

    param_names = list(sig.parameters.keys())
    defaults = {n: p.default for n, p in sig.parameters.items()
                if p.default is not inspect.Parameter.empty}

    # Fast path 1: single Request parameter with no hints → pass through
    if len(param_names) == 1 and not hints and next(iter(sig.parameters.values())).annotation is inspect.Parameter.empty:
        if is_async:
            async def pass_through_async(req):
                return await handler(req)
            pass_through_async.__name__ = handler.__name__
            return pass_through_async
        else:
            return handler  # no wrapping needed

    # Fast path 2: build a specialised resolver
    resolvers = []
    for name in param_names:
        if name == "req" or hints.get(name) is Request:
            resolvers.append(("req", name, None))
            continue
        hint = hints.get(name)
        default = defaults.get(name)
        resolvers.append(("auto", name, hint, default))

    if is_async:
        async def inject_async(req):
            return await handler(**_bind(req, resolvers))
        inject_async.__name__ = handler.__name__
        return inject_async

    def inject_sync(req):
        return handler(**_bind(req, resolvers))
    inject_sync.__name__ = handler.__name__
    return inject_sync


def _bind(req: Request, resolvers: list) -> dict[str, Any]:
    """Resolve handler kwargs from Request (path params take precedence)."""
    kwargs: dict[str, Any] = {}
    for r in resolvers:
        if r[0] == "req":
            kwargs[r[1]] = req
            continue
        _, name, hint, default = r
        if name in req.params and req.params[name] != "":
            val, from_path = req.params[name], True
        elif name in req.query:
            val, from_path = req.query[name][0] or default, False
        else:
            val, from_path = default, False
        if val is not None and hint:
            val = _cast(val, hint, name, from_path)
        kwargs[name] = val
    return kwargs


def _cast(value: Any, hint: type, name: str = "", from_path: bool = False) -> Any:
    if hint is int:
        try:
            return int(value)
        except (TypeError, ValueError):
            raise _invalid(name, value, from_path) from None
    if hint is float:
        try:
            return float(value)
        except (TypeError, ValueError):
            raise _invalid(name, value, from_path) from None
    if hint is bool:
        return value if isinstance(value, bool) else value.lower() in ("true", "1", "yes")
    return value


def _invalid(name: str, value: Any, from_path: bool):
    """Invalid typed parameter → 404 for path params (ASVS 2.1.1), 400 for query."""
    if from_path:
        return PathParamError(f"Invalid path parameter '{name}': {value!r}")
    return RequestError(f"Invalid query parameter '{name}': {value!r}")
