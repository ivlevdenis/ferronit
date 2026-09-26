"""OpenAPI 3.0 schema generator — auto-detects types from signatures."""

from __future__ import annotations

import inspect
import json as _json
from typing import Any, get_type_hints

__all__ = ["OpenAPI", "SchemaBuilder"]


class SchemaBuilder:
    """Build OpenAPI schema objects from Pydantic models."""

    @staticmethod
    def from_model(model: type) -> dict[str, Any]:
        if hasattr(model, "model_json_schema"):
            return SchemaBuilder._clean(model.model_json_schema())
        if hasattr(model, "__dataclass_fields__"):
            return SchemaBuilder._from_dataclass(model)
        return {"type": "object"}

    @staticmethod
    def _clean(schema: dict) -> dict:
        result = {}
        for k, v in schema.items():
            if k in ("$defs", "title"):
                continue
            if k == "properties":
                result[k] = {p: SchemaBuilder._clean(vv) for p, vv in v.items()}
            elif isinstance(v, dict):
                result[k] = SchemaBuilder._clean(v)
            else:
                result[k] = v
        return result

    @staticmethod
    def _from_dataclass(model: type) -> dict:
        props = {n: {"type": "string"} for n in model.__dataclass_fields__}
        return {"type": "object", "properties": props}


class OpenAPI:
    """OpenAPI 3.0 builder with auto type introspection."""

    __slots__ = ("_title", "_version", "_description", "_paths", "_schemas")

    def __init__(self, title="Velox API", version="0.1.0", description=""):
        self._title = title
        self._version = version
        self._description = description
        self._paths: dict = {}
        self._schemas: dict = {}

    def add_route(self, method: str, path: str, handler, summary="", tags=None):
        openapi_path = path
        if openapi_path not in self._paths:
            self._paths[openapi_path] = {}

        operation: dict[str, Any] = {"responses": {"200": {"description": "OK"}}}
        if summary:
            operation["summary"] = summary
        if tags:
            operation["tags"] = tags

        response_model = _get_return_type(handler)
        if response_model is not None:
            schema = SchemaBuilder.from_model(response_model)
            name = response_model.__name__
            self._schemas[name] = schema
            operation["responses"]["200"]["content"] = {
                "application/json": {"schema": {"$ref": f"#/components/schemas/{name}"}}
            }

        self._paths[openapi_path][method.lower()] = operation

    def build(self) -> dict[str, Any]:
        return {
            "openapi": "3.0.3",
            "info": {"title": self._title, "version": self._version, "description": self._description},
            "paths": self._paths,
            "components": {"schemas": self._schemas} if self._schemas else {},
        }

    def json(self, indent=2) -> str:
        return _json.dumps(self.build(), ensure_ascii=False, indent=indent)


def _get_return_type(handler) -> type | None:
    try:
        return get_type_hints(handler).get("return")
    except Exception:
        pass
    try:
        sig = inspect.signature(handler)
        if sig.return_annotation is not inspect.Signature.empty:
            return sig.return_annotation
    except Exception:
        pass
    return None
