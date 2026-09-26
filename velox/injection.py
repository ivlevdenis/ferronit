"""Auto-injection — zero-overhead: pre-computed at registration time."""

from __future__ import annotations

import inspect
from typing import Any, get_type_hints

from velox.core.request import Request

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
            kwargs = {}
            for r in resolvers:
                if r[0] == "req":
                    kwargs[r[1]] = req
                else:
                    _, name, hint, default = r
                    val = req.params.get(name) or (req.query.get(name, [None])[0] if name in req.query else None) or default
                    if val is not None and hint:
                        val = _cast(val, hint)
                    kwargs[name] = val
            return await handler(**kwargs)
        inject_async.__name__ = handler.__name__
        return inject_async
    else:
        def inject_sync(req):
            kwargs = {}
            for r in resolvers:
                if r[0] == "req":
                    kwargs[r[1]] = req
                else:
                    _, name, hint, default = r
                    val = req.params.get(name) or (req.query.get(name, [None])[0] if name in req.query else None) or default
                    if val is not None and hint:
                        val = _cast(val, hint)
                    kwargs[name] = val
            return handler(**kwargs)
        inject_sync.__name__ = handler.__name__
        return inject_sync


def _cast(value: str, hint: type) -> Any:
    if hint is int: return int(value)
    if hint is float: return float(value)
    if hint is bool: return value.lower() in ("true", "1", "yes")
    return value
