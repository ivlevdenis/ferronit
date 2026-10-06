"""Модели ``rawmodel`` как ответ JSON: Rust-энкодер пишет их без промежуточного dict.

Проверяет нативный путь ``ferrox._core`` — поля берутся из ``__columns__`` (модель) или
``__dataclass_fields__`` (обычный dataclass) и пишутся прямо в JSON. Отдельно фиксируется
порядок полей и то, что скаляры/строки не сломались после смены диспетчеризации типов.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from httpx import ASGITransport, AsyncClient

from ferrox import Ferrox
from ferrox.contrib.rawmodel import Model


class User(Model):
    id: int = 0
    name: str = ""
    email: str = ""


@dataclass
class Plain:
    id: int
    name: str


@pytest.fixture
def app():
    v = Ferrox()

    @v.route("/one")
    async def one(req):
        return User(1, "a", "a@example.com")

    @v.route("/many")
    async def many(req):
        return {"rows": [User(1, "a", "a@x"), User(2, "b", "b@x")]}

    @v.route("/nested")
    async def nested(req):
        return {"rows": [{"user": User(3, "c", "c@x")}]}

    @v.route("/plain")
    async def plain(req):
        return Plain(7, "d")

    @v.route("/scalars")
    async def scalars(req):
        return {"s": 'q" \\ \n\t', "ok": True, "n": None, "i": -5, "f": 1.5}

    return v


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_single_model_is_json_object(client) -> None:
    response = await client.get("/one")
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"id": 1, "name": "a", "email": "a@example.com"}


async def test_model_fields_keep_declaration_order(client) -> None:
    assert (await client.get("/one")).text == '{"id":1,"name":"a","email":"a@example.com"}'


async def test_models_inside_container(client) -> None:
    assert (await client.get("/many")).json() == {
        "rows": [
            {"id": 1, "name": "a", "email": "a@x"},
            {"id": 2, "name": "b", "email": "b@x"},
        ]
    }


async def test_model_nested_in_dict(client) -> None:
    assert (await client.get("/nested")).json() == {
        "rows": [{"user": {"id": 3, "name": "c", "email": "c@x"}}]
    }


async def test_plain_dataclass(client) -> None:
    assert (await client.get("/plain")).json() == {"id": 7, "name": "d"}


async def test_scalars_and_escaping_survive_dispatch_change(client) -> None:
    assert (await client.get("/scalars")).json() == {
        "s": 'q" \\ \n\t',
        "ok": True,
        "n": None,
        "i": -5,
        "f": 1.5,
    }
