"""OpenAPI 3.0 schema generator — auto-detects types from signatures."""

from __future__ import annotations

import inspect
import json as _json
from typing import Any, get_type_hints

__all__ = ["OpenAPI", "SchemaBuilder"]


class SchemaBuilder:
    """Build OpenAPI schema fragments from Python model types.

    Pydantic models and dataclasses are both supported; :meth:`from_model`
    picks the right strategy automatically.
    """

    @staticmethod
    def from_model(model: type) -> dict[str, Any]:
        """Convert a model type into an OpenAPI schema object.

        Pydantic models are rendered through ``model_json_schema`` with internal
        keys (``$defs``, ``title``) stripped; dataclasses become an object whose
        fields are all typed as strings. Any other type yields a bare object
        schema.

        Args:
            model: A Pydantic model class, a dataclass, or any other type.

        Returns:
            dict[str, Any]: An OpenAPI schema fragment.
        """
        if hasattr(model, "model_json_schema"):
            return SchemaBuilder._clean(model.model_json_schema())
        if hasattr(model, "__dataclass_fields__"):
            return SchemaBuilder._from_dataclass(model)
        columns = getattr(model, "__columns__", None) or getattr(model, "__struct_fields__", None)
        if columns is not None:
            return {"type": "object", "properties": {n: {"type": "string"} for n in columns}}
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
        fields = getattr(model, "__dataclass_fields__", {})
        props = {n: {"type": "string"} for n in fields}
        return {"type": "object", "properties": props}


class OpenAPI:
    """OpenAPI 3.0 document builder with automatic type introspection.

    Routes are accumulated with :meth:`add_route`; :meth:`build` assembles the
    document and :meth:`json` serialises it.

    Args:
        title: Value placed in ``info.title``.
        version: Value placed in ``info.version``.
        description: Value placed in ``info.description``.
    """

    __slots__ = ("_description", "_paths", "_schemas", "_title", "_version")

    def __init__(self, title="Ferronit API", version="0.1.0", description=""):
        self._title = title
        self._version = version
        self._description = description
        self._paths: dict = {}
        self._schemas: dict = {}

    def add_route(self, method: str, path: str, handler, summary="", tags=None):
        """Record one operation for a path and HTTP method.

        The handler's return annotation is introspected: when it is a model, its
        schema is stored under ``components/schemas`` and referenced from the
        ``200`` response.

        Args:
            method: HTTP method, e.g. ``"GET"``; stored lower-cased.
            path: Route path, used verbatim as the OpenAPI path key.
            handler: The route handler whose return type is introspected.
            summary: Optional operation summary.
            tags: Optional list of tag names.
        """
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
        """Assemble the OpenAPI 3.0 document.

        Returns:
            dict[str, Any]: The document with ``info``, ``paths`` and, when any
            schemas were collected, ``components``.
        """
        return {
            "openapi": "3.0.3",
            "info": {"title": self._title, "version": self._version, "description": self._description},
            "paths": self._paths,
            "components": {"schemas": self._schemas} if self._schemas else {},
        }

    def json(self, indent=2) -> str:
        """Serialise the OpenAPI document to a JSON string.

        Args:
            indent: Indentation level passed to :func:`json.dumps`.

        Returns:
            str: The pretty-printed OpenAPI document.
        """
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
