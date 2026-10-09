"""Auto-injection — zero-overhead: pre-computed at registration time."""

from __future__ import annotations

import inspect
import types
from typing import Annotated, Any, Union, get_args, get_origin, get_type_hints

from ferronit.core.request import PathParamError, Request, RequestError

__all__ = ["Header", "inject"]

_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})


class Header:
    """Маркер: параметр читается из HTTP-заголовка, а не из query/path.

    ``Header()`` — имя заголовка совпадает с именем параметра;
    ``Header("x-api-key")`` — явное имя заголовка. Используется через
    ``typing.Annotated``: ``api_key: Annotated[str, Header("x-api-key")] = ""``.
    """

    __slots__ = ("name",)

    def __init__(self, name: str | None = None) -> None:
        self.name = name


def inject(handler):
    """Wrap handler with pre-computed parameter resolution strategy.

    Fast paths (chosen at registration time, zero per-request overhead):
        1. pass_through — handler expects Request directly;
        2. inject_params — handler has typed path/query/header/body params;
        3. default — fallback.

    Для каждого параметра на регистрации собирается коэрсер-замыкание и источник
    (path/query/header/body) — на каждый запрос остаётся только вызов готовой
    функции, без ``get_origin``/``isinstance`` в рантайме.
    """
    sig = inspect.signature(handler)
    try:
        hints = get_type_hints(handler, include_extras=True)
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
    # Резолвер — (kind, name, coercer, default, is_list, element_coercer, extra):
    #   kind="req"    → сам Request;
    #   kind="header" → extra = имя заголовка;
    #   kind="body"   → extra = класс модели (await req.model);
    #   kind="auto"   → path/query.
    resolvers: list[tuple[str, str, Any, Any, bool, Any, Any]] = []
    body_resolvers: list[tuple[str, type]] = []
    for name in param_names:
        if name == "req" or hints.get(name) is Request:
            resolvers.append(("req", name, None, None, False, None, None))
            continue
        hint = hints.get(name)
        default = defaults.get(name, inspect.Parameter.empty)
        base, header = _unwrap_annotated(hint)
        if header is not None:
            resolvers.append((
                "header", name, _make_coercer(base), default, False, None, header.name or name,
            ))
            continue
        if _is_body_model(base):
            resolvers.append(("body", name, None, default, False, None, base))
            body_resolvers.append((name, base))
            continue
        element = _element_type(base)
        resolvers.append((
            "auto",
            name,
            _make_coercer(base),
            default,
            element is not None,
            _make_coercer(element) if element is not None else None,
            None,
        ))

    if is_async:
        async def inject_async(req):
            body = {n: await req.model(m) for n, m in body_resolvers} if body_resolvers else {}
            return await handler(*_resolve(req, resolvers, body))
        inject_async.__name__ = handler.__name__
        return inject_async

    if body_resolvers:
        raise TypeError(
            f"{handler.__name__}: body-параметры требуют async-хендлер "
            f"(нельзя await в sync-функции)"
        )

    def inject_sync(req):
        return handler(*_resolve(req, resolvers, {}))
    inject_sync.__name__ = handler.__name__
    return inject_sync


def _unwrap_annotated(hint: Any) -> tuple[Any, Header | None]:
    """Развернуть ``Annotated[T, Header(...)]`` → ``(T, Header)`` либо ``(hint, None)``."""
    if get_origin(hint) is Annotated:
        base = get_args(hint)[0]
        for meta in get_args(hint)[1:]:
            if isinstance(meta, Header):
                return base, meta
        return base, None
    return hint, None


def _is_body_model(hint: Any) -> bool:
    """True, если hint — модель тела запроса (msgspec.Struct/dataclass/pydantic)."""
    return isinstance(hint, type) and (
        hasattr(hint, "__struct_fields__")
        or hasattr(hint, "__dataclass_fields__")
        or hasattr(hint, "model_validate")
    )


def _element_type(hint: Any) -> Any | None:
    """Return the element type if ``hint`` is ``list[T]``, else ``None``."""
    if get_origin(hint) is list:
        args = get_args(hint)
        return args[0] if args else None
    return None


def _make_coercer(hint: Any):
    """Build a value coercer for a type hint once, at registration time.

    Returns a callable ``(value, name, source) -> Any``, or ``None`` for
    pass-through types (``str`` and anything unknown). Union members are tried
    in declaration order; ``NoneType`` is skipped (``None`` comes from the
    default, not from the request).
    """
    if hint is None:
        return None
    if hint is int:
        def to_int(value, name="", source="query"):
            try:
                return int(value)
            except (TypeError, ValueError):
                raise _invalid(name, value, source) from None
        return to_int
    if hint is float:
        def to_float(value, name="", source="query"):
            try:
                return float(value)
            except (TypeError, ValueError):
                raise _invalid(name, value, source) from None
        return to_float
    if hint is bool:
        def to_bool(value, name="", source="query"):
            if isinstance(value, bool):
                return value
            lowered = value.lower() if isinstance(value, str) else str(value).lower()
            if lowered in _TRUE:
                return True
            if lowered in _FALSE:
                return False
            raise _invalid(name, value, source)
        return to_bool
    origin = get_origin(hint)
    if origin in (types.UnionType, Union):
        members = [m for m in get_args(hint) if m is not type(None)]
        coercers = [_make_coercer(m) for m in members]

        def to_union(value, name="", source="query"):
            for coercer in coercers:
                try:
                    if coercer is None:
                        return value  # str / неизвестный тип — как есть
                    return coercer(value, name, source)
                except (RequestError, PathParamError):
                    continue
            raise _invalid(name, value, source)
        return to_union
    return None


def _resolve(req: Request, resolvers: list, body_values: dict) -> list[Any]:
    """Resolve handler arguments in signature order (positional, no kwargs)."""
    values: list[Any] = []
    for r in resolvers:
        kind = r[0]
        if kind == "req":
            values.append(req)
            continue
        _, name, coercer, default, is_list, element_coercer, extra = r
        if kind == "body":
            values.append(body_values[name])
            continue
        if kind == "header":
            value = req.get_header(extra)
            if value is None:
                if default is inspect.Parameter.empty:
                    raise RequestError(f"Missing header parameter '{name}'")
                value = default
            if value is not None and coercer is not None:
                value = coercer(value, name, "header")
            values.append(value)
            continue
        # auto: path params имеют приоритет над query
        if is_list:
            if name in req.params and req.params[name] != "":
                raw, source = [req.params[name]], "path"
            elif name in req.query:
                raw, source = list(req.query[name]), "query"
            elif isinstance(default, (list, tuple)):
                raw, source = list(default), "query"
            else:
                raw, source = [], "query"
            if element_coercer is not None:
                values.append([element_coercer(v, name, source) for v in raw])
            else:
                values.append(raw)
            continue
        if name in req.params and req.params[name] != "":
            val, source, present = req.params[name], "path", True
        elif name in req.query:
            val, source, present = req.query[name][0], "query", True
        else:
            val, source, present = default, "query", False
        if not present:
            if default is inspect.Parameter.empty:
                raise RequestError(f"Missing {source} parameter '{name}'")
            val = default
        else:
            if val == "" and default is not inspect.Parameter.empty:
                val = default
            if val is not None and coercer is not None:
                val = coercer(val, name, source)
        values.append(val)
    return values


def _invalid(name: str, value: Any, source: str):
    """Invalid typed parameter → 404 (path) or 400 (query/header)."""
    if source == "path":
        return PathParamError(f"Invalid path parameter '{name}': {value!r}")
    if source == "header":
        return RequestError(f"Invalid header parameter '{name}': {value!r}")
    return RequestError(f"Invalid query parameter '{name}': {value!r}")
