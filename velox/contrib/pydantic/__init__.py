"""Serialization layer — protocol-based codecs for request/response bodies.

Supported codecs: Pydantic v2 (pydantic), attrs (attrs + cattrs).
All are optional — Velox core has zero dependencies.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = [
    "Codec",
    "PydanticCodec",
    "register_codec",
    "get_codec",
    "decode_json",
    "encode_json",
]


# ── Protocol ──────────────────────────────────────────────────────────

@runtime_checkable
class Codec(Protocol):
    """Serialisation protocol — any type with these classmethods works."""

    @staticmethod
    def encode(obj: Any) -> dict[str, Any] | list[Any]: ...

    @staticmethod
    def decode(data: dict[str, Any] | list[Any], target: type) -> Any: ...


# ── Registry ──────────────────────────────────────────────────────────

_codecs: dict[type, Codec] = {}


def register_codec(model_type: type, codec: Codec) -> None:
    """Register a codec for a given model base class."""
    _codecs[model_type] = codec


def get_codec(model_type: type) -> Codec | None:
    """Find the best codec for a type."""
    # Direct match
    if model_type in _codecs:
        return _codecs[model_type]
    # Walk MRO
    for base in model_type.__mro__:
        if base in _codecs:
            return _codecs[base]
    return None


# ── Convenience ───────────────────────────────────────────────────────

def decode_json(data: dict[str, Any] | list[Any], target: type) -> Any:
    """Decode JSON-like data into target model."""
    codec = get_codec(target)
    if codec is not None:
        return codec.decode(data, target)
    # Fallback: simple dataclass / plain dict
    if hasattr(target, "__dataclass_fields__"):
        return target(**{k: v for k, v in data.items() if k in target.__dataclass_fields__})
    return data


def encode_json(obj: Any) -> dict[str, Any] | list[Any]:
    """Encode a model instance to JSON-serialisable dict/list."""
    codec = get_codec(type(obj))
    if codec is not None:
        return codec.encode(obj)
    if hasattr(obj, "model_dump"):  # Pydantic auto-detect
        return obj.model_dump()
    if hasattr(obj, "dict"):  # Pydantic v1 / common
        return obj.dict()
    return obj
