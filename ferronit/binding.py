"""Handler argument binding — request params + container DI, pre-computed at registration."""

from __future__ import annotations

import inspect
import re
import types
from typing import Annotated, Any, Union, get_args, get_origin, get_type_hints

import msgspec

from ferronit.core.request import PathParamError, Request, RequestError
from ferronit.errors import (
    INVALID_TYPE,
    MISSING,
    NOT_MULTIPLE_OF,
    PATTERN_MISMATCH,
    TOO_LARGE,
    TOO_LONG,
    TOO_SHORT,
    TOO_SMALL,
    FieldError,
    ValidationError,
)

__all__ = ["Body", "Header", "Query", "bind", "build", "plan"]

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


class Query:
    """Маркер: параметр — query-модель (``msgspec.Struct``), поля маппятся на query-параметры.

    Невалидное значение (тип или констрейнт ``msgspec.Meta``) откатывается к
    default поля; если default нет — ``RequestError`` (400). Используется через
    ``typing.Annotated``: ``filters: Annotated[SearchQuery, Query()]``.
    """

    __slots__ = ()


class Body:
    """Маркер: параметр — body-модель (явное указание; модель без маркера и так body)."""

    __slots__ = ()


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
        base, marker = _unwrap_annotated(hint)
        if isinstance(marker, Header):
            resolvers.append((
                "header", name, _make_coercer(base), default, False, None, marker.name or name,
            ))
            continue
        if isinstance(marker, Query):
            resolvers.append(("query_model", name, None, default, False, None, base))
            continue
        if isinstance(marker, Body):
            resolvers.append(("body", name, None, default, False, None, base))
            body_resolvers.append((name, base))
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


def _unwrap_annotated(hint: Any) -> tuple[Any, Any]:
    """Развернуть ``Annotated[T, Маркер(...)]`` → ``(T, маркер)`` либо ``(hint, None)``.

    Маркер — ``Header`` / ``Query`` / ``Body``.
    """
    if get_origin(hint) is Annotated:
        base = get_args(hint)[0]
        for meta in get_args(hint)[1:]:
            if isinstance(meta, (Header, Query, Body)):
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
                except RequestError:
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
        if kind == "query_model":
            values.append(_resolve_query_model(req, extra))
            continue
        if kind == "body":
            values.append(body_values[name])
            continue
        if kind == "header":
            value = req.get_header(extra)
            if value is None:
                if default is inspect.Parameter.empty:
                    raise ValidationError([
                        FieldError(MISSING, f"Missing header parameter '{name}'", name)
                    ])
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
                raise ValidationError([
                    FieldError(MISSING, f"Missing {source} parameter '{name}'", name)
                ])
            val = default
        else:
            if val == "" and default is not inspect.Parameter.empty:
                val = default
            if val is not None and coercer is not None:
                val = coercer(val, name, source)
        values.append(val)
    return values


def _invalid(name: str, value: Any, source: str):
    """Invalid typed parameter → 404 (path) or 400 (query/header), with a code."""
    message = f"Invalid {source} parameter '{name}': {value!r}"
    error = FieldError(INVALID_TYPE, message, name)
    if source == "path":
        return PathParamError(message, errors=[error])
    return ValidationError([error])


# ── Query-модель (msgspec.Struct → query-параметры) ────────────────────


def _split_constraint(t: Any) -> tuple[Any, Any]:
    """Развернуть ``Annotated[T, msgspec.Meta(...)]`` → ``(T, Meta)`` либо ``(t, None)``."""
    if get_origin(t) is Annotated:
        args = get_args(t)
        base = args[0]
        for meta in args[1:]:
            if isinstance(meta, msgspec.Meta):
                return base, meta
        return base, None
    return t, None


def _constraint_error(name: str, value: Any, meta: Any) -> FieldError | None:
    """Code and message for the first violated ``msgspec.Meta`` constraint.

    Args:
        name: Query parameter name (becomes ``FieldError.field``).
        value: Coerced value to check.
        meta: ``msgspec.Meta`` of the annotation, or ``None``.

    Returns:
        The matching FieldError, or ``None`` when every constraint holds.
    """
    if meta is None:
        return None
    prefix = f"Invalid query parameter '{name}'"
    if meta.ge is not None and value < meta.ge:
        return FieldError(TOO_SMALL, f"{prefix}: must be >= {meta.ge}", name)
    if meta.gt is not None and value <= meta.gt:
        return FieldError(TOO_SMALL, f"{prefix}: must be > {meta.gt}", name)
    if meta.le is not None and value > meta.le:
        return FieldError(TOO_LARGE, f"{prefix}: must be <= {meta.le}", name)
    if meta.lt is not None and value >= meta.lt:
        return FieldError(TOO_LARGE, f"{prefix}: must be < {meta.lt}", name)
    if meta.multiple_of is not None and value % meta.multiple_of != 0:
        return FieldError(
            NOT_MULTIPLE_OF, f"{prefix}: must be a multiple of {meta.multiple_of}", name
        )
    if meta.min_length is not None and len(value) < meta.min_length:
        return FieldError(TOO_SHORT, f"{prefix}: length must be >= {meta.min_length}", name)
    if meta.max_length is not None and len(value) > meta.max_length:
        return FieldError(TOO_LONG, f"{prefix}: length must be <= {meta.max_length}", name)
    if meta.pattern is not None and re.match(meta.pattern, str(value)) is None:
        return FieldError(PATTERN_MISMATCH, f"{prefix}: must match {meta.pattern!r}", name)
    return None


def _coerce_field(raw: list[str], base: Any) -> Any:
    """Привести список query-значений к базовому типу (скаляр или ``list[T]``)."""
    element = _element_type(base)
    if element is not None:
        coercer = _make_coercer(element)
        return [coercer(v, "", "query") if coercer is not None else v for v in raw]
    coercer = _make_coercer(base)
    v = raw[0]
    return coercer(v, "", "query") if coercer is not None else v


def _resolve_query_model(req: Request, model_cls: type) -> Any:
    """Build a query model from the query string, field by field.

    An invalid value (wrong type or violated ``msgspec.Meta`` constraint) falls
    back to the field default; if the field has no default the error is collected
    and every such error is reported at once (``ValidationError`` → 400).

    Args:
        req: Current request.
        model_cls: ``msgspec.Struct`` with the query fields.

    Returns:
        The built model instance.

    Raises:
        ValidationError: If a field without a default is missing or invalid.
    """
    values: dict[str, Any] = {}
    errors: list[FieldError] = []
    for f in msgspec.structs.fields(model_cls):
        name = f.name
        has_default = (
            f.default is not msgspec.NODEFAULT
            or f.default_factory is not msgspec.NODEFAULT
        )
        if name not in req.query:
            if not has_default:
                errors.append(
                    FieldError(MISSING, f"Missing query parameter '{name}'", name)
                )
            continue
        base, meta = _split_constraint(f.type)
        try:
            value = _coerce_field(req.query[name], base)
        except (RequestError, ValueError, TypeError):
            value = None
        if value is None:
            if not has_default:
                errors.append(
                    FieldError(INVALID_TYPE, f"Invalid query parameter '{name}'", name)
                )
            continue
        violated = _constraint_error(name, value, meta)
        if violated is not None:
            if not has_default:
                errors.append(violated)
            continue
        values[name] = value
    if errors:
        raise ValidationError(errors)
    return model_cls(**values)
