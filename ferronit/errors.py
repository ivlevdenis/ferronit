"""Structured request errors — a machine-readable code per error, one envelope.

Every rejected request answers with the same JSON shape::

    {
      "code": "validation_error",
      "message": "Request validation failed: 2 errors",
      "errors": [
        {"code": "missing", "field": "name", "message": "Field 'name' is required"},
        {"code": "too_small", "field": "age", "message": "Expected `int` >= 1"}
      ]
    }

``errors`` holds both field errors (they set ``field``) and errors that are not
about a field at all — malformed JSON, a body that is not an object, an
unsupported media type, an unknown route. Those omit ``field``.

Codes are stable strings: clients switch on ``code`` instead of matching prose.
"""

from __future__ import annotations

import re
import types
import typing
from typing import Any

import msgspec

__all__ = [
    "BODY_TOO_LARGE",
    "INTERNAL_ERROR",
    "INVALID_ENUM",
    "INVALID_JSON",
    "INVALID_REQUEST",
    "INVALID_TYPE",
    "INVALID_VALUE",
    "MISSING",
    "NOT_FOUND",
    "NOT_MULTIPLE_OF",
    "PATTERN_MISMATCH",
    "REQUEST_ERRORS",
    "TOO_LARGE",
    "TOO_LONG",
    "TOO_SHORT",
    "TOO_SMALL",
    "UNSUPPORTED_MEDIA_TYPE",
    "VALIDATION_ERROR",
    "BodyTooLarge",
    "FieldError",
    "InternalError",
    "NotFoundError",
    "PathParamError",
    "RequestError",
    "UnsupportedMediaType",
    "ValidationError",
    "classify",
    "collect_errors",
    "from_msgspec",
    "validation_error_for_body",
]

# ── Коды ошибок (публичный контракт: клиент переключается по ним) ──────
MISSING = "missing"
INVALID_TYPE = "invalid_type"
INVALID_VALUE = "invalid_value"
TOO_SMALL = "too_small"
TOO_LARGE = "too_large"
TOO_SHORT = "too_short"
TOO_LONG = "too_long"
NOT_MULTIPLE_OF = "not_multiple_of"
PATTERN_MISMATCH = "pattern_mismatch"
INVALID_ENUM = "invalid_enum"
INVALID_JSON = "invalid_json"
INVALID_REQUEST = "invalid_request"
VALIDATION_ERROR = "validation_error"
NOT_FOUND = "not_found"
UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
BODY_TOO_LARGE = "body_too_large"
INTERNAL_ERROR = "internal_error"

#: Все коды, которые может отдать фреймворк (для доков и тестов).
REQUEST_ERRORS = (
    MISSING, INVALID_TYPE, INVALID_VALUE, TOO_SMALL, TOO_LARGE, TOO_SHORT, TOO_LONG,
    NOT_MULTIPLE_OF, PATTERN_MISMATCH, INVALID_ENUM, INVALID_JSON, INVALID_REQUEST,
    VALIDATION_ERROR, NOT_FOUND, UNSUPPORTED_MEDIA_TYPE, BODY_TOO_LARGE, INTERNAL_ERROR,
)


class FieldError(msgspec.Struct, omit_defaults=True):
    """One rejected input.

    Args:
        code: Machine-readable reason from :data:`REQUEST_ERRORS`.
        message: Human-readable text (English, safe to log).
        field: Dotted path of the rejected input (``name``, ``addr.zip``,
            ``tags[0]``); ``None`` for errors that are not about a field.
    """

    code: str
    message: str
    field: str | None = None


class RequestError(Exception):
    """A client-side problem → JSON error response.

    Args:
        message: Human-readable text; also the top-level ``message``.
        code: Overrides the class-level code when given.
        errors: Granular errors for the ``errors`` array; when empty, one entry
            is synthesised from ``(code, message)``.
    """

    status = 400
    code = INVALID_REQUEST

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        errors: list[FieldError] | tuple[FieldError, ...] | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.errors: list[FieldError] = list(errors or ())

    def payload(self) -> dict[str, Any]:
        """Build the response body: ``code``, ``message`` and always ``errors``.

        Returns:
            dict: JSON-ready payload; ``errors`` is never empty.
        """
        errors = self.errors or [FieldError(self.code, str(self))]
        return {"code": self.code, "message": str(self), "errors": errors}


class ValidationError(RequestError):
    """One or more inputs failed validation → 400 with a code per error."""

    code = VALIDATION_ERROR

    def __init__(
        self,
        errors: list[FieldError] | tuple[FieldError, ...],
        message: str | None = None,
    ) -> None:
        errors = list(errors)
        if message is None:
            count = len(errors)
            message = f"Request validation failed: {count} error" + ("" if count == 1 else "s")
        super().__init__(message, errors=errors)


class PathParamError(RequestError):
    """A path parameter failed type coercion → 404 (ASVS 2.1.1).

    A malformed path parameter must not reveal that the route exists, so the
    status is the same as for an unknown route.
    """

    status = 404
    code = NOT_FOUND


class NotFoundError(RequestError):
    """No route matches the request → 404."""

    status = 404
    code = NOT_FOUND


class UnsupportedMediaType(RequestError):
    """Content-Type is not supported for the requested operation → 415."""

    status = 415
    code = UNSUPPORTED_MEDIA_TYPE


class BodyTooLarge(RequestError):
    """Request body exceeds ``max_body_size`` → 413."""

    status = 413
    code = BODY_TOO_LARGE


class InternalError(RequestError):
    """An unhandled handler error → 500 (message never leaks the traceback)."""

    status = 500
    code = INTERNAL_ERROR


# ── Классификация сообщений msgspec ────────────────────────────────────

#: ``Expected `str` of length >= 2`` / ``... <= 4``
_LENGTH_RE = re.compile(r" of length (>=|<=|>|<) ")
#: ``Expected `int` >= 1``
_BOUND_RE = re.compile(r"(>=|<=|>|<) ")
#: ``Expected `int`, got `str` - at `$.age```
_AT_RE = re.compile(r"^(.*) - at `\$(.*)`$", re.DOTALL)


def classify(message: str) -> str:
    """Map a msgspec validation message to a stable error code.

    Args:
        message: Text of ``msgspec.ValidationError``.

    Returns:
        One of the ``REQUEST_ERRORS`` codes.
    """
    if message.startswith("Object missing required field"):
        return MISSING
    if message.startswith("Invalid enum value"):
        return INVALID_ENUM
    if "matching regex" in message:
        return PATTERN_MISMATCH
    if "multiple of" in message:
        return NOT_MULTIPLE_OF
    length = _LENGTH_RE.search(message)
    if length:
        return TOO_SHORT if length.group(1).startswith(">") else TOO_LONG
    if message.startswith("Expected") and ", got " in message:
        return INVALID_TYPE
    bound = _BOUND_RE.search(message)
    if bound:
        return TOO_SMALL if bound.group(1).startswith(">") else TOO_LARGE
    return INVALID_VALUE


def from_msgspec(exc: Exception, field: str = "") -> FieldError:
    """Turn a ``msgspec.ValidationError`` into a :class:`FieldError`.

    The JSON pointer msgspec appends (``- at `$.addr.zip```) becomes the dotted
    field path; ``$.tags[0]`` keeps its index, ``$.d[...]`` (dict value) is
    trimmed to the dict itself.

    Args:
        exc: The msgspec error.
        field: Prefix path of the value being validated.

    Returns:
        FieldError with code, cleaned message and absolute field path.
    """
    text = str(exc)
    code = classify(text)
    match = _AT_RE.match(text)
    if match:
        text, suffix = match.group(1), match.group(2)
        if suffix.startswith("."):
            suffix = suffix[1:]
        suffix = suffix.replace("[...]", "")
        if suffix:
            if suffix.startswith("["):
                field = f"{field}{suffix}"
            else:
                field = f"{field}.{suffix}" if field else suffix
    return FieldError(code, text, field or None)


# ── Сбор ошибок по модели (все сразу, а не первая) ─────────────────────


def _split_annotated(hint: Any) -> tuple[Any, msgspec.Meta | None]:
    """Unwrap ``Annotated[T, msgspec.Meta(...)]`` → ``(T, Meta | None)``."""
    if getattr(hint, "__metadata__", None) is None:
        return hint, None
    if typing.get_origin(hint) is not typing.Annotated:
        return hint, None
    meta = next((m for m in hint.__metadata__ if isinstance(m, msgspec.Meta)), None)
    return hint.__origin__, meta


def _is_struct(t: Any) -> bool:
    """Whether ``t`` is a ``msgspec.Struct`` subclass."""
    return isinstance(t, type) and hasattr(t, "__struct_fields__")


def _optional_struct(t: Any) -> type | None:
    """For ``Struct | None`` return the Struct type (nested ``Optional`` structs)."""
    if typing.get_origin(t) not in (typing.Union, types.UnionType):
        return None
    members = [m for m in typing.get_args(t) if m is not type(None)]
    if len(members) != 1:
        return None
    base, _meta = _split_annotated(members[0])
    return base if _is_struct(base) else None


def _struct_item(t: Any) -> type | None:
    """For ``list[Struct]`` / ``tuple[Struct, ...]`` return the item Struct type."""
    origin = getattr(t, "__origin__", None)
    if origin not in (list, tuple):
        return None
    args = getattr(t, "__args__", ())
    if not args:
        return None
    base, _ = _split_annotated(args[0])
    return base if _is_struct(base) else None


def _has_default(field: msgspec.structs.FieldInfo) -> bool:
    """Whether a struct field has a default or a default factory."""
    return field.default is not msgspec.NODEFAULT or field.default_factory is not msgspec.NODEFAULT


def _json_type(value: Any) -> str:
    """Name of a JSON value's type, msgspec style (``object``, ``array``, ...)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "int"
    return "float"


def collect_errors(
    data: Any, model: type, prefix: str = ""
) -> tuple[Any | None, list[FieldError]]:
    """Validate ``data`` against a ``msgspec.Struct`` model, collecting every error.

    msgspec stops at the first failure (``json.decode`` is fail-fast); this walker
    visits every declared field — including nested structs and struct lists — so
    the response lists all mistakes at once with absolute paths.

    Args:
        data: Decoded JSON value (usually a dict).
        model: ``msgspec.Struct`` subclass to build.
        prefix: Path prefix for nested calls (``addr`` → ``addr.zip``).

    Returns:
        tuple: ``(instance, [])`` when everything passed,
        ``(None, errors)`` otherwise.
    """
    if not isinstance(data, dict):
        return None, [FieldError(INVALID_TYPE, f"Expected `object`, got `{_json_type(data)}`", prefix or None)]

    errors: list[FieldError] = []
    values: dict[str, Any] = {}
    for field in msgspec.structs.fields(model):
        key = field.encode_name or field.name
        path = f"{prefix}.{key}" if prefix else key
        if key not in data:
            if not _has_default(field):
                errors.append(FieldError(MISSING, f"Field '{path}' is required", path))
            continue
        raw = data[key]
        base, _meta = _split_annotated(field.type)
        nested_type = base if _is_struct(base) else _optional_struct(base)
        if nested_type is not None:
            if raw is None:
                values[key] = None
                continue
            if isinstance(raw, dict):
                nested, nested_errors = collect_errors(raw, nested_type, path)
                errors.extend(nested_errors)
                if nested is not None:
                    values[key] = nested
                continue
            errors.append(
                FieldError(
                    INVALID_TYPE,
                    f"Expected `object`, got `{_json_type(raw)}`",
                    path,
                )
            )
            continue
        items = _struct_item(base)
        if items is not None and isinstance(raw, list):
            built: list[Any] = []
            for index, item in enumerate(raw):
                if not isinstance(item, dict):
                    errors.append(
                        FieldError(
                            INVALID_TYPE,
                            f"Expected `object`, got `{_json_type(item)}`",
                            f"{path}[{index}]",
                        )
                    )
                    continue
                nested, nested_errors = collect_errors(item, items, f"{path}[{index}]")
                errors.extend(nested_errors)
                if nested is not None:
                    built.append(nested)
            if len(built) == len(raw):
                values[key] = built
            continue
        try:
            values[key] = msgspec.convert(raw, field.type)
        except msgspec.ValidationError as exc:
            errors.append(from_msgspec(exc, path))
        except TypeError as exc:  # unsupported annotation — не ошибка входа
            errors.append(FieldError(INVALID_VALUE, str(exc), path))
    if errors:
        return None, errors
    try:
        return model(**values), []
    except TypeError as exc:  # несогласованные default'ы модели — вина кода, не входа
        return None, [FieldError(INVALID_VALUE, str(exc))]


def validation_error_for_body(body: bytes, model: type, first: Exception) -> RequestError:
    """Rebuild a full error list for a body that failed msgspec's fast decode.

    Args:
        body: Raw request body (already read).
        model: Target ``msgspec.Struct``.
        first: The ``ValidationError``/``DecodeError`` from the fast path.

    Returns:
        ValidationError with every error, or a plain RequestError for malformed
        JSON (a non-field error).
    """
    try:
        data = msgspec.json.decode(body)
    except msgspec.DecodeError as exc:
        return RequestError(f"Malformed JSON body: {exc}", code=INVALID_JSON)
    _value, errors = collect_errors(data, model)
    if not errors:
        errors = [from_msgspec(first)]  # страховка: fast path упал, обход — нет
    return ValidationError(errors)
