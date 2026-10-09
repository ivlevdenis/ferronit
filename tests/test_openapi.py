"""Генерация OpenAPI-схемы из сигнатур и типов."""

import json
from dataclasses import dataclass

import pytest

from ferronit.openapi import OpenAPI, SchemaBuilder


@dataclass
class UserOut:
    user_id: int
    name: str


def test_minimal_document_shape():
    spec = OpenAPI("Test API", "1.2.3", "demo").build()

    assert spec["openapi"] == "3.0.3"
    assert spec["info"] == {"title": "Test API", "version": "1.2.3", "description": "demo"}
    assert spec["paths"] == {}
    assert spec["components"] == {}


def test_add_route_records_summary_and_tags():
    api = OpenAPI()

    def handler(user_id: int):
        return {"user_id": user_id}

    api.add_route("GET", "/users/{user_id}", handler, summary="Get user", tags=["users"])
    operation = api.build()["paths"]["/users/{user_id}"]["get"]

    assert operation["summary"] == "Get user"
    assert operation["tags"] == ["users"]
    assert operation["responses"]["200"]["description"] == "OK"
    assert "content" not in operation["responses"]["200"]  # модель ответа не объявлена


def test_dataclass_return_type_becomes_component_schema():
    api = OpenAPI()

    def handler() -> UserOut:
        return UserOut(1, "A")

    api.add_route("GET", "/user", handler, summary="Current user")
    spec = api.build()

    ref = spec["paths"]["/user"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert ref == {"$ref": "#/components/schemas/UserOut"}
    assert spec["components"]["schemas"]["UserOut"]["properties"] == {
        "user_id": {"type": "string"},
        "name": {"type": "string"},
    }


def test_methods_are_grouped_by_path():
    api = OpenAPI()
    api.add_route("GET", "/cart", lambda: {})
    api.add_route("POST", "/cart", lambda: {})

    assert set(api.build()["paths"]["/cart"]) == {"get", "post"}


def test_pydantic_schema_drops_title_and_defs():
    pydantic = pytest.importorskip("pydantic")

    class Item(pydantic.BaseModel):
        name: str
        qty: int

    schema = SchemaBuilder.from_model(Item)

    assert "title" not in schema
    assert "$defs" not in schema
    assert set(schema["properties"]) == {"name", "qty"}


def test_unknown_model_falls_back_to_object():
    assert SchemaBuilder.from_model(int) == {"type": "object"}


def test_json_output_is_valid_and_keeps_unicode():
    api = OpenAPI()
    api.add_route("GET", "/привет", lambda: {"ok": True}, summary="Юникод")

    data = json.loads(api.json())

    assert "/привет" in data["paths"]
    assert data["paths"]["/привет"]["get"]["summary"] == "Юникод"
