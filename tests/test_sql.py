"""SQL node tree and dialects — `ferronit/contrib/sql.py`.

Проверяем две вещи: (1) паритет — PostgreSQL-рендер совпадает с прежним выводом
строчного генератора (строки из tests/test_rawmodel.py повторены здесь как
опорные), (2) переносимость — та же запись компилируется во второй диалект.
"""

from __future__ import annotations

import pytest

from ferronit.contrib import sql
from ferronit.contrib.rawdb import Condition
from ferronit.contrib.rawmodel import Model, select


class User(Model):
    id: int = 0
    name: str = ""
    email: str = ""


# ── выражения ────────────────────────────────────────────────────────────────


def test_compare_renders_per_dialect() -> None:
    node = sql.Compare("age", ">", 18)
    assert sql.compile_expr(node, sql.POSTGRES) == ("age > $1", [18])
    assert sql.compile_expr(node, sql.SQLITE) == ('"age" > ?', [18])


def test_values_stay_bound() -> None:
    """Значение не попадает в SQL — только в параметры."""
    node = sql.Compare("name", "=", "O'Brien; DROP TABLE users")
    sql_text, params = sql.compile_expr(node, sql.POSTGRES)
    assert sql_text == "name = $1"
    assert params == ["O'Brien; DROP TABLE users"]


def test_compare_with_none_is_null_check() -> None:
    assert sql.compile_expr(sql.Compare("email", "=", None)) == ("email IS NULL", [])
    assert sql.compile_expr(sql.Compare("email", "!=", None)) == (
        "email IS NOT NULL",
        [],
    )


def test_is_null_like_and_between() -> None:
    assert sql.compile_expr(sql.IsNull("email", negated=True)) == (
        "email IS NOT NULL",
        [],
    )
    assert sql.compile_expr(sql.Like("name", "%a%")) == ("name LIKE $1", ["%a%"])
    assert sql.compile_expr(sql.Like("name", "%a%", insensitive=True)) == (
        "name ILIKE $1",
        ["%a%"],
    )
    assert sql.compile_expr(sql.Between("age", 5, 10)) == (
        "age BETWEEN $1 AND $2",
        [5, 10],
    )


def test_ilike_falls_back_to_like_without_ilike() -> None:
    node = sql.Like("name", "%a%", insensitive=True)
    assert sql.compile_expr(node, sql.SQLITE) == ('"name" LIKE ?', ["%a%"])


def test_bool_ops_are_binary_and_parenthesised() -> None:
    """`a & b & c` вкладывается влево — форма сохранена от строкового генератора."""
    a = sql.Compare("a", ">", 1)
    b = sql.Compare("b", "=", 2)
    c = sql.Compare("c", "<", 3)
    assert sql.compile_expr(a & b & c) == (
        "((a > $1) AND (b = $2)) AND (c < $3)",
        [1, 2, 3],
    )
    assert sql.compile_expr(a | (b & c)) == (
        "(a > $1) OR ((b = $2) AND (c < $3))",
        [1, 2, 3],
    )
    assert sql.compile_expr(~sql.IsNull("email")) == ("NOT (email IS NULL)", [])


def test_paren_node_wraps() -> None:
    assert sql.compile_expr(sql.Paren(sql.Compare("a", ">", 1))) == (
        "(a > $1)",
        [1],
    )


def test_raw_fragment_is_renumbered() -> None:
    """Сырой фрагмент сдвигается на число уже связанных параметров."""
    node = sql.And(sql.Compare("a", ">", 1), sql.Raw("b = $1 AND b < $2", [2, 9]))
    assert sql.compile_expr(node) == ("(a > $1) AND (b = $2 AND b < $3)", [1, 2, 9])


def test_raw_fragment_keeps_two_digit_placeholders() -> None:
    """`$1` не должен затирать начало `$10`."""
    condition = Condition("a = $1", [1])
    node = sql.And(condition, sql.Raw("b = $10", list(range(10))))
    sql_text, params = sql.compile_expr(node)
    assert sql_text == "(a = $1) AND (b = $11)"
    assert params == [1, *range(10)]


def test_placeholder_reuse_duplicated_on_question_mark_dialects() -> None:
    """Диалект без ссылок на параметр получает его копию на каждое вхождение."""
    sql_text, params = sql.compile_expr(sql.Raw("a = $1 AND b = $1", [5]), sql.SQLITE)
    assert sql_text == "a = ? AND b = ?"
    assert params == [5, 5]


def test_condition_object_is_accepted_as_raw_fragment() -> None:
    """`Condition` (сырой SQL) — легальный лист дерева."""
    sql_text, params = sql.compile_expr(Condition("age > $1", [18]))
    assert (sql_text, params) == ("age > $1", [18])


def test_unknown_node_is_rejected() -> None:
    with pytest.raises(TypeError):
        sql.compile_expr(object())


# ── IN: ANY против IN (...) ──────────────────────────────────────────────────


def test_in_list_uses_any_on_postgres() -> None:
    node = sql.InList("id", [1, 2, 3])
    assert sql.compile_expr(node) == ("id = ANY($1)", [[1, 2, 3]])
    assert sql.compile_expr(sql.InList("id", [1, 2], negated=True)) == (
        "NOT (id = ANY($1))",
        [[1, 2]],
    )


def test_in_list_expands_where_any_is_unsupported() -> None:
    node = sql.InList("id", [1, 2, 3])
    assert sql.compile_expr(node, sql.SQLITE) == ('"id" IN (?, ?, ?)', [1, 2, 3])


# ── операторы (statement) ────────────────────────────────────────────────────


def test_select_matches_legacy_shape() -> None:
    sql_text, params = select(User).where(User.c.name == "A", User.c.id > 5).limit(10).compile()
    assert sql_text == (
        "SELECT id, name, email FROM users"
        " WHERE (name = $1) AND (id > $2) LIMIT $3"
    )
    assert params == ["A", 5, 10]


def test_select_binds_where_before_page() -> None:
    sql_text, params = select(User).where(User.c.id > 5).limit(10).offset(2).compile()
    assert sql_text == "SELECT id, name, email FROM users WHERE (id > $1) LIMIT $2 OFFSET $3"
    assert params == [5, 10, 2]


def test_select_statement_without_where() -> None:
    assert sql.compile_statement(sql.Select("users", ("*",))) == (
        "SELECT * FROM users",
        [],
    )


def test_select_statement_for_sqlite() -> None:
    statement = sql.Select(
        "users", ("id", "name"), where=[sql.Compare("id", "=", 7)], limit=5
    )
    assert sql.compile_statement(statement, sql.SQLITE) == (
        'SELECT id, name FROM "users" WHERE "id" = ? LIMIT ?',
        [7, 5],
    )


def test_insert_upsert_returning_for_postgres() -> None:
    assert sql.compile_statement(
        sql.Insert("users", ["name", "email"], returning=("id", "name", "email"))
    ) == ("INSERT INTO users (name, email) VALUES ($1, $2) RETURNING id, name, email", [])

    assert sql.compile_statement(
        sql.Insert("users", ["id", "name"], conflict_key="id", returning=("id", "name"))
    ) == (
        "INSERT INTO users (id, name) VALUES ($1, $2)"
        " ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name RETURNING id, name",
        [],
    )


def test_insert_many_rows_numbers_placeholders_continuously() -> None:
    sql_text, _ = sql.compile_statement(
        sql.Insert("users", ["name"], rows=3, returning=("id",))
    )
    assert sql_text == "INSERT INTO users (name) VALUES ($1), ($2), ($3) RETURNING id"


def test_insert_rejects_empty_row_count() -> None:
    with pytest.raises(ValueError):
        sql.compile_statement(sql.Insert("users", ["name"], rows=0))


def test_update_and_delete() -> None:
    assert sql.compile_statement(
        sql.Update("users", ["name", "email"], "id", returning=("id", "name"))
    ) == (
        "UPDATE users SET name = $1, email = $2 WHERE id = $3 RETURNING id, name",
        [],
    )
    assert sql.compile_statement(sql.Delete("users", "id")) == (
        "DELETE FROM users WHERE id = $1",
        [],
    )


# ── диалекты и их ограничения ────────────────────────────────────────────────


def test_upsert_requires_dialect_support() -> None:
    statement = sql.Insert("users", ["id", "name"], conflict_key="id")
    with pytest.raises(sql.DialectError):
        sql.compile_statement(statement, sql.Dialect())


def test_returning_requires_dialect_support() -> None:
    statement = sql.Insert("users", ["name"], returning=("id",))
    with pytest.raises(sql.DialectError):
        sql.compile_statement(statement, sql.Dialect())


def test_dialects_declare_their_features() -> None:
    assert sql.POSTGRES.supports_any and sql.POSTGRES.supports_returning
    assert not sql.SQLITE.supports_any and sql.SQLITE.supports_returning
    assert not sql.MYSQL.supports_any
    assert not sql.MYSQL.supports_returning
    assert sql.MYSQL.supports_upsert and sql.SQLITE.supports_upsert


def test_upsert_syntax_differs_per_dialect() -> None:
    """У каждой СУБД свой синтаксис конфликта — рендер живёт в диалекте."""
    statement = sql.Insert("users", ["id", "name"], conflict_key="id")
    assert sql.compile_statement(statement, sql.POSTGRES)[0].endswith(
        "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name"
    )
    assert sql.compile_statement(statement, sql.SQLITE)[0].endswith(
        'ON CONFLICT ("id") DO UPDATE SET "name" = excluded."name"'
    )
    assert sql.compile_statement(statement, sql.MYSQL)[0].endswith(
        "ON DUPLICATE KEY UPDATE `name` = VALUES(`name`)"
    )


def test_mysql_placeholders_quotes_and_lists() -> None:
    node = sql.And(sql.Compare("age", ">", 18), sql.InList("name", ["a", "b"]))
    assert sql.compile_expr(node, sql.MYSQL) == (
        "(`age` > %s) AND (`name` IN (%s, %s))",
        [18, "a", "b"],
    )


def test_mysql_has_no_ilike_and_no_returning() -> None:
    assert sql.compile_expr(sql.Like("name", "%a%", insensitive=True), sql.MYSQL) == (
        "`name` LIKE %s",
        ["%a%"],
    )
    with pytest.raises(sql.DialectError):
        sql.compile_statement(
            sql.Insert("users", ["name"], returning=("id",)), sql.MYSQL
        )


def test_mysql_statements() -> None:
    statement = sql.Select(
        "users", ("id", "name"), where=[sql.Compare("id", "=", 3)], limit=5, offset=10
    )
    assert sql.compile_statement(statement, sql.MYSQL) == (
        "SELECT id, name FROM `users` WHERE `id` = %s LIMIT %s OFFSET %s",
        [3, 5, 10],
    )
    assert sql.compile_statement(sql.Update("users", ["name"], "id"), sql.MYSQL) == (
        "UPDATE `users` SET `name` = %s WHERE `id` = %s",
        [],
    )
    assert sql.compile_statement(sql.Delete("users", "id"), sql.MYSQL) == (
        "DELETE FROM `users` WHERE `id` = %s",
        [],
    )


def test_query_can_be_compiled_for_mysql() -> None:
    sql_text, params = select(User).where(User.c.id > 5).limit(2).compile(sql.MYSQL)
    assert sql_text == "SELECT id, name, email FROM `users` WHERE (`id` > %s) LIMIT %s"
    assert params == [5, 2]


# ── группировки, агрегаты, DISTINCT ──────────────────────────────────────────


def test_func_renders_per_dialect() -> None:
    assert sql.compile_expr(sql.Func("count", "*")) == ("count(*)", [])
    assert sql.compile_expr(sql.Func("sum", "age")) == ("sum(age)", [])
    assert sql.compile_expr(sql.Func("sum", "age"), sql.SQLITE) == ('sum("age")', [])
    assert sql.compile_expr(sql.Func("max", "age"), sql.MYSQL) == ("max(`age`)", [])


def test_aggregate_in_having_condition() -> None:
    node = sql.Compare(sql.Func("count", "*"), ">", 2)
    assert sql.compile_expr(node) == ("count(*) > $1", [2])
    assert sql.compile_expr(node, sql.SQLITE) == ("count(*) > ?", [2])
    assert sql.compile_expr(node, sql.MYSQL) == ("count(*) > %s", [2])


def test_select_distinct_group_by_having() -> None:
    statement = sql.Select(
        "users",
        ("name", sql.Func("count", "*")),
        distinct=True,
        group_by=("name",),
        having=[sql.Compare(sql.Func("count", "*"), ">", 1)],
        order=("name",),
    )
    assert sql.compile_statement(statement) == (
        "SELECT DISTINCT name, count(*) FROM users"
        " GROUP BY name HAVING count(*) > $1 ORDER BY name",
        [1],
    )
    assert sql.compile_statement(statement, sql.SQLITE) == (
        'SELECT DISTINCT name, count(*) FROM "users"'
        ' GROUP BY name HAVING count(*) > ? ORDER BY name',
        [1],
    )
    assert sql.compile_statement(statement, sql.MYSQL) == (
        "SELECT DISTINCT name, count(*) FROM `users`"
        " GROUP BY name HAVING count(*) > %s ORDER BY name",
        [1],
    )


def test_query_grouping_api_compiles() -> None:
    query = (
        select(User)
        .columns("name", sql.Func("count", "*"))
        .where(User.c.id > 0)
        .group_by(User.c.name)
        .having(sql.Compare(sql.Func("count", "*"), ">", 1))
        .order_by(User.c.name)
        .limit(10)
    )
    sql_text, params = query.compile()
    assert sql_text == (
        "SELECT name, count(*) FROM users WHERE (id > $1)"
        " GROUP BY name HAVING (count(*) > $2) ORDER BY name LIMIT $3"
    )
    assert params == [0, 1, 10]

    lite_sql, lite_params = query.compile(sql.SQLITE)
    assert lite_sql == (
        'SELECT name, count(*) FROM "users" WHERE ("id" > ?)'
        ' GROUP BY name HAVING (count(*) > ?) ORDER BY name LIMIT ?'
    )
    assert lite_params == [0, 1, 10]


def test_parity_kept_when_grouping_unused() -> None:
    """Без группировок компиляция не изменилась — паритет с прежним выводом."""
    assert select(User).compile()[0] == "SELECT id, name, email FROM users"
    assert select(User).count().compile()[0] == "SELECT count(*) FROM users"


# ── интеграция с Query ───────────────────────────────────────────────────────


def test_query_can_be_compiled_for_another_dialect() -> None:
    query = select(User).where(User.c.id > 5).limit(2)
    sql_text, params = query.compile(sql.SQLITE)
    assert sql_text == 'SELECT id, name, email FROM "users" WHERE ("id" > ?) LIMIT ?'
    assert params == [5, 2]


def test_query_dialect_at_construction() -> None:
    query = select(User, dialect=sql.SQLITE).where(User.c.id.in_([1, 2]))
    sql_text, params = query.compile()
    assert sql_text == 'SELECT id, name, email FROM "users" WHERE ("id" IN (?, ?))'
    assert params == [1, 2]


def test_dsl_filter_compiles_for_both_dialects() -> None:
    expression = "id > 1 and (name ilike '%a%' or email in ('x', 'y'))"
    pg_sql, pg_params = select(User).where(expression).compile()
    assert pg_sql == (
        "SELECT id, name, email FROM users"
        " WHERE ((id > $1) AND ((name ILIKE $2) OR (email = ANY($3))))"
    )
    assert pg_params == [1, "%a%", ["x", "y"]]

    lite_sql, lite_params = select(User).where(expression).compile(sql.SQLITE)
    assert lite_sql == (
        'SELECT id, name, email FROM "users"'
        ' WHERE (("id" > ?) AND (("name" LIKE ?) OR ("email" IN (?, ?))))'
    )
    assert lite_params == [1, "%a%", "x", "y"]


# ── Ref, Join и подзапрос ────────────────────────────────────────────────────


def test_ref_qualifies_and_quotes_per_dialect() -> None:
    """``Ref`` добавляет алиас; имя колонки квотится по диалекту."""
    assert sql.compile_expr(sql.Ref("id", "t0")) == ("t0.id", [])
    assert sql.compile_expr(sql.Ref("id", "t0"), sql.SQLITE) == ('t0."id"', [])
    assert sql.compile_expr(sql.Ref("id"), sql.MYSQL) == ("`id`", [])


def test_compare_of_two_refs_binds_nothing() -> None:
    """Колонка против колонки — это SQL в тексте, а не bound-параметр."""
    node = sql.Compare(sql.Ref("parent_id", "t1"), "=", sql.Ref("id", "t0"))
    assert sql.compile_expr(node) == ("t1.parent_id = t0.id", [])


def test_join_renders_every_kind() -> None:
    on = sql.Compare(sql.Ref("parent_id", "t1"), "=", sql.Ref("id", "t0"))
    for kind in sql.JOIN_KINDS:
        statement = sql.Select(
            "parents",
            [sql.Ref("id", "t0")],
            joins=[sql.Join("children", on, kind=kind, alias="t1")],
        )
        assert sql.compile_statement(statement) == (
            f"SELECT t0.id FROM parents {kind} JOIN children t1 ON t1.parent_id = t0.id",
            [],
        )


def test_full_join_is_refused_by_mysql() -> None:
    """MySQL не умеет FULL OUTER JOIN — диалект отказывает, а не врёт SQL-ом."""
    statement = sql.Select(
        "parents",
        ["id"],
        joins=[sql.Join("children", sql.Raw("1 = 1"), kind=sql.FULL, alias="t1")],
    )
    assert sql.compile_statement(statement)[0] == (
        "SELECT id FROM parents FULL JOIN children t1 ON 1 = 1"
    )
    with pytest.raises(sql.DialectError):
        sql.compile_statement(statement, sql.MYSQL)


def test_unknown_join_kind_is_rejected() -> None:
    with pytest.raises(ValueError):
        sql.Join("children", sql.Raw("1 = 1"), kind="CROSS")


def test_join_without_condition_is_rejected() -> None:
    with pytest.raises(ValueError):
        sql.Join("children", None)


def test_join_alias_must_be_given_once() -> None:
    """Алиас — либо у таблицы, либо у подзапроса: два сразу дали бы `t1 t1`."""
    on = sql.Compare(sql.Ref("user_id", "t1"), "=", sql.Ref("id", "t0"))
    with pytest.raises(ValueError):
        sql.Join("posts", on)
    derived = sql.Subquery(sql.Select("posts", ["id"]), "t1")
    with pytest.raises(ValueError):
        sql.Join(derived, on, alias="t1")


def test_join_to_a_derived_table() -> None:
    """Джойн к подзапросу: обе стороны — ``Subquery`` со своими параметрами."""
    recent = sql.Subquery(
        sql.Select("posts", ["id", "user_id"], where=[sql.Compare("views", ">", 10)]), "t1"
    )
    statement = sql.Select(
        "users",
        [sql.Ref("id", "t0")],
        joins=[
            sql.Join(
                recent,
                sql.Compare(sql.Ref("user_id", "t1"), "=", sql.Ref("id", "t0")),
                kind=sql.INNER,
            )
        ],
        where=[sql.Compare(sql.Ref("name", "t0"), "=", "Ann")],
    )
    assert sql.compile_statement(statement) == (
        "SELECT t0.id FROM users INNER JOIN "
        "(SELECT id, user_id FROM posts WHERE views > $1) t1 ON t1.user_id = t0.id "
        "WHERE t0.name = $2",
        [10, "Ann"],
    )


def test_subquery_keeps_placeholders_continuous() -> None:
    """Параметры подзапроса идут в общий список — $N не сбивается."""
    inner = sql.Select("users", ["id"], where=[sql.Compare("age", ">", 30)], limit=5)
    statement = sql.Select(
        sql.Subquery(inner, "t0"),
        [sql.Ref("id", "t0")],
        joins=[
            sql.Join(
                "posts",
                sql.Compare(sql.Ref("user_id", "t1"), "=", sql.Ref("id", "t0")),
                alias="t1",
            )
        ],
        where=[sql.Compare(sql.Ref("id", "t0"), "<", 100)],
    )
    assert sql.compile_statement(statement) == (
        "SELECT t0.id FROM (SELECT id FROM users WHERE age > $1 LIMIT $2) t0 "
        "LEFT JOIN posts t1 ON t1.user_id = t0.id WHERE t0.id < $3",
        [30, 5, 100],
    )
    assert sql.compile_statement(statement, sql.MYSQL) == (
        "SELECT t0.`id` FROM (SELECT id FROM `users` WHERE `age` > %s LIMIT %s) t0 "
        "LEFT JOIN `posts` t1 ON t1.`user_id` = t0.`id` WHERE t0.`id` < %s",
        [30, 5, 100],
    )
