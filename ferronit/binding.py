"""Handler argument binding — request params + container DI, pre-computed at registration."""

from __future__ import annotations

import inspect
import types
from typing import Annotated, Any, Union, get_args, get_origin, get_type_hints

from ferronit.core.request import PathParamError, Request, RequestError

__all__ = ["Header", "bind", "build", "plan"]

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


class _Plan:
    """Pre-computed handler binding plan (classification, no wrapper yet)."""

    __slots__ = ("body_resolvers", "di_keys", "is_async", "pass_through", "resolvers")

    def __init__(self, resolvers, body_resolvers, is_async, pass_through, di_keys) -> None:
        self.resolvers = resolvers
        self.body_resolvers = body_resolvers
        self.is_async = is_async
        self.pass_through = pass_through
        self.di_keys = di_keys


def plan(handler, container=None) -> _Plan:
    """Classify a handler's parameters into a bind plan.

    The plan records, for each parameter, where its value comes from
    (request / header / body / DI container / path-query). DI parameters are
    resolved later — at startup — so the same plan can be built into a wrapper
    with pre-bound dependencies.

    Args:
        handler: The route handler function.
        container: Optional DI container; DI params are those whose type or
            ``Annotated[...]`` key is registered in it.

    Returns:
        A :class:`_Plan` ready for :func:`build`.
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

    # Fast path: single Request parameter with no hints → pass through
    if (
        len(param_names) == 1
        and not hints
        and next(iter(sig.parameters.values())).annotation is inspect.Parameter.empty
    ):
        return _Plan([], [], is_async, True, [])

    # Резолвер — (kind, name, coercer, default, is_list, element_coercer, extra):
    #   kind="req"    → сам Request;
    #   kind="header" → extra = имя заголовка;
    #   kind="body"   → extra = класс модели (await req.model);
    #   kind="di"     → extra = ключ контейнера;
    #   kind="auto"   → path/query.
    resolvers: list[tuple[str, str, Any, Any, bool, Any, Any]] = []
    body_resolvers: list[tuple[str, type]] = []
    di_keys: list[tuple[str, Any]] = []
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
        # DI-зависимость: тип/Annotated-ключ зарегистрирован в контейнере
        if container is not None and hint is not None and container.has(hint):
            resolvers.append(("di", name, None, default, False, None, hint))
            di_keys.append((name, hint))
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

    return _Plan(resolvers, body_resolvers, is_async, False, di_keys)


def build(handler, plan: _Plan, container=None, resolved: dict | None = None):
    """Build the handler wrapper from a :func:`plan`.

    ``resolved`` maps DI parameter names to already-resolved instances; those
    parameters become constants ("value" resolvers), so the wrapper does no
    per-request ``container.get`` for them.

    Args:
        handler: The route handler function.
        plan: The :class:`_Plan` produced by :func:`plan`.
        container: DI container used for any unresolved DI parameters.
        resolved: Optional ``{param_name: instance}`` for pre-bound DI deps.

    Returns:
        A wrapper callable ready to be registered as a route handler.
    """
    if plan.pass_through:
        if plan.is_async:
            async def pass_through_async(req):
                return await handler(req)
            pass_through_async.__name__ = handler.__name__
            return pass_through_async
        return handler  # no wrapping needed

    resolvers = plan.resolvers
    if resolved:
        resolvers = [
            ("value", r[1], None, r[3], False, None, resolved[r[1]])
            if r[0] == "di" and r[1] in resolved
            else r
            for r in plan.resolvers
        ]

    # Все параметры — заранее подставленные константы → прямой вызов без резолвера.
    if resolvers and all(r[0] == "value" for r in resolvers):
        values = tuple(r[6] for r in resolvers)
        if plan.is_async:
            async def direct_async(req):
                return await handler(*values)
            direct_async.__name__ = handler.__name__
            return direct_async
        def direct_sync(req):
            return handler(*values)
        direct_sync.__name__ = handler.__name__
        return direct_sync

    if plan.is_async:
        async def bind_async(req):
            body = {n: await req.model(m) for n, m in plan.body_resolvers} if plan.body_resolvers else {}
            return await handler(*_bind_values(req, resolvers, body, container))
        bind_async.__name__ = handler.__name__
        return bind_async

    if plan.body_resolvers:
        raise TypeError(
            f"{handler.__name__}: body-параметры требуют async-хендлер "
            f"(нельзя await в sync-функции)"
        )

    def bind_sync(req):
        return handler(*_bind_values(req, resolvers, {}, container))
    bind_sync.__name__ = handler.__name__
    return bind_sync


def bind(handler, container=None):
    """Convenience wrapper: :func:`plan` + :func:`build` with lazy DI resolution."""
    return build(handler, plan(handler, container), container=container)


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


def _bind_values(req: Request, resolvers: list, body_values: dict, container=None) -> list[Any]:
    """Resolve handler arguments in signature order (positional, no kwargs)."""
    values: list[Any] = []
    for r in resolvers:
        kind = r[0]
        if kind == "req":
            values.append(req)
            continue
        if kind == "di":
            values.append(container.get(r[6]))
            continue
        if kind == "value":
            values.append(r[6])
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
