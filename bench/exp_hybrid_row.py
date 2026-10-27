"""Можно ли сделать класс, который сразу и ``asyncpg.Record``, и ``msgspec.Struct``.

Проверка идеи «промежуточный класс между Struct и Record»: если бы драйвер заполнял
модель сам, шаг маппинга (7,8 µs на 100 строк, 74 µs на 1000 — см. RESULTS_hybrid_row.md)
исчез бы. Здесь три опыта: слияние базовых классов, что asyncpg разрешает в
``record_class`` и что msgspec вообще умеет кодировать.

Запуск: .venv/bin/python bench/exp_hybrid_row.py
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import NamedTuple

import asyncpg
import msgspec

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"


class SlotStruct:
    """Класс со слотами и ``__struct_fields__`` — «вьюха», которую хочется кодировать."""

    __slots__ = ("id", "name", "email")
    __struct_fields__ = ("id", "name", "email")

    def __init__(self, row_id: int, name: str, email: str) -> None:
        self.id, self.name, self.email = row_id, name, email


class SlotView:
    """Вьюха над строкой драйвера: хранит ссылку, значения не копирует."""

    __slots__ = ("_row",)
    __struct_fields__ = ("id", "name", "email")

    def __init__(self, row) -> None:
        self._row = row


class NT(NamedTuple):
    """namedtuple: msgspec кодирует его, но массивом, а не объектом."""

    id: int
    name: str
    email: str


@dataclass
class DC:
    """dataclass: msgspec кодирует объектом (как Struct, но своим, более медленным путём)."""

    id: int
    name: str
    email: str


def check_encode() -> None:
    """Опыт 1: что msgspec вообще готов кодировать."""
    print("=== 1. что msgspec умеет кодировать ===")
    probes = (
        ("класс со слотами + __struct_fields__", SlotStruct(1, "a", "b@x")),
        ("вьюха над строкой + __struct_fields__", SlotView([1, "a", "b@x"])),
        ("namedtuple", NT(1, "a", "b@x")),
        ("dataclass", DC(1, "a", "b@x")),
        ("tuple", (1, "a", "b@x")),
    )
    for label, obj in probes:
        try:
            print(f"  {label:38s} -> {msgspec.json.encode(obj)}")
        except Exception as exc:
            print(f"  {label:38s} -> {type(exc).__name__}: {str(exc)[:70]}")


def check_hybrid_class() -> None:
    """Опыт 2: можно ли слить Struct и Record в один класс."""
    print("\n=== 2. слияние Struct и Record ===")
    for label, bases in (
        ("Record, Struct", (asyncpg.Record, msgspec.Struct)),
        ("Struct, Record", (msgspec.Struct, asyncpg.Record)),
    ):
        try:
            cls = type("Hybrid", bases, {"__annotations__": {"id": int}, "id": 0})
            print(f"  {label}: создан {cls}")
        except Exception as exc:
            print(f"  {label}: {type(exc).__name__}: {str(exc)[:90]}")


class RecMethods(asyncpg.Record):
    """Подкласс Record только с методами — asyncpg такое принимает."""

    __slots__ = ()

    def as_dict(self) -> dict:
        """Показать, что подкласс доехал до приложения вместе со своим методом."""
        return dict(self)


class RecInit(asyncpg.Record):
    """Подкласс Record со своим конструктором — так делать нельзя."""

    def __init__(self, *args) -> None:  # noqa: D107
        pass


async def check_record_class() -> None:
    """Опыт 3: что asyncpg разрешает в ``record_class``."""
    print("\n=== 3. что asyncpg разрешает в record_class ===")
    conn = await asyncpg.connect(DSN)
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS bench_rows ("
        "id serial PRIMARY KEY, name text NOT NULL, email text NOT NULL)"
    )
    if await conn.fetchval("SELECT count(*) FROM bench_rows") == 0:
        await conn.execute(
            "INSERT INTO bench_rows (name, email) "
            "SELECT 'user_' || g, 'u' || g || '@example.com' FROM generate_series(1, 1000) g"
        )
    sql = "SELECT id, name, email FROM bench_rows LIMIT 3"
    try:
        rows = await conn.fetch(sql, record_class=RecMethods)
        print(f"  методы в подклассе: ок — {type(rows[0]).__name__} {rows[0].as_dict()}")
    except Exception as exc:
        print(f"  методы в подклассе: {type(exc).__name__}: {str(exc)[:90]}")
    try:
        await conn.fetch(sql, record_class=RecInit)
        print("  свой конструктор: неожиданно разрешён")
    except Exception as exc:
        print(f"  свой конструктор: {type(exc).__name__}: {str(exc)[:90]}")
    rec = await conn.fetchrow(sql)
    for label, obj in (("Record", rec), ("list(Record)", list(rec)), ("dict(Record)", dict(rec))):
        try:
            print(f"  msgspec.json.encode({label}) -> {msgspec.json.encode(obj)}")
        except Exception as exc:
            print(f"  msgspec.json.encode({label}) -> {type(exc).__name__}: {str(exc)[:60]}")
    await conn.close()


def main() -> None:
    """Гоняет три опыта подряд."""
    check_encode()
    check_hybrid_class()
    asyncio.run(check_record_class())


if __name__ == "__main__":
    main()
