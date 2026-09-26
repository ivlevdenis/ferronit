"""Pydantic v2 codec — validates on decode, dumps on encode."""

from __future__ import annotations

from typing import Any

import pydantic

from velox.contrib.pydantic import Codec, register_codec

__all__ = ["PydanticCodec", "install"]


class PydanticCodec(Codec):
    """Codec for Pydantic v2 BaseModel."""

    @staticmethod
    def encode(obj: Any) -> dict[str, Any] | list[Any]:
        return obj.model_dump()

    @staticmethod
    def decode(data: dict[str, Any] | list[Any], target: type) -> Any:
        return target.model_validate(data)


def install() -> None:
    """Register Pydantic codec for all BaseModel subclasses."""
    register_codec(pydantic.BaseModel, PydanticCodec())
