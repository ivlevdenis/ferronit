"""Auto-injection — zero-overhead: pre-computed at registration time."""

from __future__ import annotations

import inspect
import types
from typing import Any, Union, get_args, get_origin, get_type_hints

from ferrox.core.request import PathParamError, Request, RequestError

__all__ = ["inject"]

_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})


def inject(handler):
    """Wrap handler with pre-computed parameter resolution strategy.

    Three fast paths (chosen at registration time, zero per-request overhead):
        1. pass_through — handler expects Request directly
        2. inject_params — handler has typed path/query params
        3. default — fallback

    Для каждого параметра на регистрации собирается коэрсер-замыкание (int/float/
    bool/union/list[T]) — на каждый запрос остаётся только вызов готовой функции,
    без ``get_origin``/``isinstance``-проверок в рантайме.
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

    # Fast path 2: build a specialised resolver.
    # Резолвер — (kind, name, coercer, default, is_list, element_coercer); всё
    # «тяжёлое» (get_origin, сборка замыканий) происходит здесь, один раз.
    resolvers: list[tuple[str, str, Any, Any, bool, Any]] = []
    for name in param_names:
        if name == "req" or hints.get(name) is Request:
            resolvers.append(("req", name, None, None, False, None))
            continue
        hint = hints.get(name)
        default = defaults.get(name)
        element = _element_type(hint)
        resolvers.append((
            "auto",
            name,
            _make_coercer(hint),
            default,
            element is not None,
            _make_coercer(element) if element is not None else None,
        ))

    if is_async:
        async def inject_async(req):
            return await handler(*_resolve(req, resolvers))
        inject_async.__name__ = handler.__name__
        return inject_async

    def inject_sync(req):
        return handler(*_resolve(req, resolvers))
    inject_sync.__name__ = handler.__name__
    return inject_sync


def _element_type(hint: Any) -> Any | None:
    """Return the element type if ``hint`` is ``list[T]``, else ``None``."""
    if get_origin(hint) is list:
        args = get_args(hint)
        return args[0] if args else None
    return None


def _make_coercer(hint: Any):
    """Build a value coercer for a type hint once, at registration time.

    Returns a callable ``(value, name, from_path) -> Any``, or ``None`` for
    pass-through types (``str`` and anything unknown — the value stays as-is).
    Union members are tried in declaration order; the first one that accepts the
    value wins, ``NoneType`` is skipped (``None`` comes from the default, not
    from the query string).
    """
    if hint is None:
        return None
    if hint is int:
        def to_int(value, name="", from_path=False):
            try:
                return int(value)
            except (TypeError, ValueError):
                raise _invalid(name, value, from_path) from None
        return to_int
    if hint is float:
        def to_float(value, name="", from_path=False):
            try:
                return float(value)
            except (TypeError, ValueError):
                raise _invalid(name, value, from_path) from None
        return to_float
    if hint is bool:
        def to_bool(value, name="", from_path=False):
            if isinstance(value, bool):
                return value
            lowered = value.lower() if isinstance(value, str) else str(value).lower()
            if lowered in _TRUE:
                return True
            if lowered in _FALSE:
                return False
            raise _invalid(name, value, from_path)
        return to_bool
    origin = get_origin(hint)
    if origin in (types.UnionType, Union):
        members = [m for m in get_args(hint) if m is not type(None)]
        coercers = [_make_coercer(m) for m in members]

        def to_union(value, name="", from_path=False):
            for coercer in coercers:
                try:
                    if coercer is None:
                        return value  # str / неизвестный тип — как есть
                    return coercer(value, name, from_path)
                except (RequestError, PathParamError):
                    continue
            raise _invalid(name, value, from_path)
        return to_union
    return None


def _resolve(req: Request, resolvers: list) -> list[Any]:
    """Resolve handler arguments in signature order (positional, no kwargs)."""
    values: list[Any] = []
    for r in resolvers:
        if r[0] == "req":
            values.append(req)
            continue
        _, name, coercer, default, is_list, element_coercer = r
        if is_list:
            # list[T]: все значения query (или одно из path), каждый элемент кастуется
            if name in req.params and req.params[name] != "":
                raw, from_path = [req.params[name]], True
            elif name in req.query:
                raw, from_path = list(req.query[name]), False
            elif isinstance(default, (list, tuple)):
                raw, from_path = list(default), False
            else:
                raw, from_path = [], False
            if element_coercer is not None:
                values.append([element_coercer(v, name, from_path) for v in raw])
            else:
                values.append(raw)
            continue
        if name in req.params and req.params[name] != "":
            val, from_path = req.params[name], True
        elif name in req.query:
            val, from_path = req.query[name][0] or default, False
        else:
            val, from_path = default, False
        if val is not None and coercer is not None:
            val = coercer(val, name, from_path)
        values.append(val)
    return values


def _invalid(name: str, value: Any, from_path: bool):
    """Invalid typed parameter → 404 for path params (ASVS 2.1.1), 400 for query."""
    if from_path:
        return PathParamError(f"Invalid path parameter '{name}': {value!r}")
    return RequestError(f"Invalid query parameter '{name}': {value!r}")
