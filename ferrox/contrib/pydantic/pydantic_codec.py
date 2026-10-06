"""Pydantic v2 codec — validates on decode, dumps on encode."""

from __future__ import annotations

from typing import Any

import pydantic

from ferrox.contrib.pydantic import Codec, register_codec

__all__ = ["PydanticCodec", "install"]


class PydanticCodec(Codec):
    """Codec for Pydantic v2 BaseModel."""

    @staticmethod
    def encode(obj: Any) -> dict[str, Any] | list[Any]:
        """Dump a Pydantic model to a plain ``dict`` via ``model_dump()``."""
        return obj.model_dump()

    @staticmethod
    def decode(data: dict[str, Any] | list[Any], target: type) -> Any:
        """Validate ``data`` into ``target`` using ``model_validate``.

        Args:
            data: Decoded JSON payload.
            target: Expected Pydantic v2 model class.

        Returns:
            An instance of ``target``.

        Raises:
            TypeError: If ``target`` is not a Pydantic v2 model.
        """
        validate = getattr(target, "model_validate", None)
        if validate is None:
            raise TypeError(f"{target!r} is not a Pydantic v2 model")
        return validate(data)


def install() -> None:
    """Register Pydantic codec for all BaseModel subclasses."""
    register_codec(pydantic.BaseModel, PydanticCodec())
