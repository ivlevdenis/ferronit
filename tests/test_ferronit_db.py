"""Тесты Rust-слоя `ferronit.db` (PostgreSQL → JSON целиком в Rust).

Если PostgreSQL недоступен на :5432, тесты пропускаются.
"""

from __future__ import annotations

import json

import pytest

from ferronit import db

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"


@pytest.fixture(scope="module", autouse=True)
def _connected():
    """Один пул на модуль; без PostgreSQL — пропуск."""
    try:
        db.connect(DSN, 4)
    except Exception as exc:  # pragma: no cover - зависит от окружения
        pytest.skip(f"PostgreSQL недоступен: {type(exc).__name__}")
    yield


def query(sql: str, params: list | None = None) -> list:
    """Выполнить запрос и вернуть распарсенный JSON."""
    return json.loads(db.query_json(sql, params or []))


def test_select_scalars() -> None:
    assert query("SELECT 1 AS i, 'x' AS s, true AS b, 1.5::float8 AS f") == [
        {"i": 1, "s": "x", "b": True, "f": 1.5}
    ]


def test_query_parameters() -> None:
    assert query(
        "SELECT $1::text AS s, $2::int4 AS i, $3::bool AS b, $4::float8 AS f, $5::int8 AS big",
        ["hello", 7, True, 2.5, 2**62],
    ) == [{"s": "hello", "i": 7, "b": True, "f": 2.5, "big": 2**62}]


def test_null_becomes_none() -> None:
    assert query("SELECT NULL::int4 AS i, NULL::text AS s") == [{"i": None, "s": None}]


def test_numeric_keeps_precision() -> None:
    rows = query(
        "SELECT 123456789012345678901234567890.123456789::numeric AS a, "
        "-0.00042::numeric AS b, 'NaN'::numeric AS c, 0::numeric AS d"
    )
    assert rows == [
        {
            "a": "123456789012345678901234567890.123456789",
            "b": "-0.00042",
            "c": "NaN",
            "d": "0",
        }
    ]


def test_json_uuid_datetime_bytea() -> None:
    rows = query(
        "SELECT '{\"a\":[1,2]}'::jsonb AS j, "
        "'550e8400-e29b-41d4-a716-446655440000'::uuid AS u, "
        "'2024-03-05'::date AS d, "
        "'2024-03-05 12:34:56+00'::timestamptz AS t, "
        "'\\xdeadbeef'::bytea AS by"
    )
    assert rows == [
        {
            "j": {"a": [1, 2]},
            "u": "550e8400-e29b-41d4-a716-446655440000",
            "d": "2024-03-05",
            "t": "2024-03-05T12:34:56+00:00",
            "by": "deadbeef",
        }
    ]


def test_prepared_statement_is_reused() -> None:
    sql = "SELECT $1::int4 AS n"
    assert query(sql, [1]) == [{"n": 1}]
    assert query(sql, [2]) == [{"n": 2}]  # второй вызов — уже кэшированный statement


def test_repeated_query_returns_same_shape() -> None:
    sql = "SELECT g AS n, 'user_' || g AS name FROM generate_series(1, 5) AS g"
    first = query(sql)
    second = query(sql)
    assert first == second
    assert len(first) == 5


def test_arrays_and_nulls_inside() -> None:
    rows = query(
        "SELECT ARRAY[1,2,3]::int4[] AS ints, "
        "ARRAY['x', NULL, 'z']::text[] AS texts, "
        "ARRAY[1.5, 2.5]::numeric[] AS nums, "
        "ARRAY['550e8400-e29b-41d4-a716-446655440000'::uuid, NULL]::uuid[] AS uuids, "
        "ARRAY[]::int4[] AS empty, NULL::text[] AS null_array"
    )
    assert rows == [
        {
            "ints": [1, 2, 3],
            "texts": ["x", None, "z"],
            "nums": ["1.5", "2.5"],
            "uuids": ["550e8400-e29b-41d4-a716-446655440000", None],
            "empty": [],
            "null_array": None,
        }
    ]


def test_invalid_sql_raises() -> None:
    with pytest.raises(RuntimeError):
        query("SELECT * FROM table_that_does_not_exist_ferronit_db")


def test_connect_twice_raises() -> None:
    with pytest.raises(RuntimeError):
        db.connect(DSN, 4)
