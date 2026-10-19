"""SQL node tree and dialects — `ferronit/contrib/sql.py`.

Проверяем две вещи: (1) паритет — PostgreSQL-рендер совпадает с прежним выводом
строчного генератора (строки из tests/test_rawmodel.py повторены здесь как
опорные), (2) переносимость — та же запись компилируется во второй диалект.
"""

from __future__ import annotations

import sys

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


# ── подзапрос в предикате ────────────────────────────────────────────────────


def test_in_subquery_keeps_placeholders_continuous() -> None:
    """`IN (SELECT …)`: значения подзапроса и наружного WHERE — один список."""
    inner = sql.Subquery(
        sql.Select("orders", ["user_id"], where=[sql.Compare("total", ">", 100)])
    )
    statement = sql.Select(
        "users",
        ["id"],
        where=[sql.InList("id", inner), sql.Compare("age", ">", 30)],
    )
    assert sql.compile_statement(statement) == (
        "SELECT id FROM users WHERE id IN (SELECT user_id FROM orders WHERE total > $1) "
        "AND age > $2",
        [100, 30],
    )


def test_not_in_subquery() -> None:
    inner = sql.Subquery(sql.Select("bans", ["user_id"]))
    assert sql.compile_expr(sql.InList("id", inner, negated=True)) == (
        "NOT (id IN (SELECT user_id FROM bans))",
        [],
    )


def test_scalar_subquery_in_comparison() -> None:
    """Скалярный подзапрос справа от сравнения — это SQL, а не параметр."""
    average = sql.Subquery(sql.Select("users", [sql.Func("avg", "age")]))
    assert sql.compile_expr(sql.Compare("age", ">", average)) == (
        "age > (SELECT avg(age) FROM users)",
        [],
    )


def test_exists_with_correlation() -> None:
    """`EXISTS` со ссылкой на наружную таблицу (коррелированный подзапрос)."""
    inner = sql.Subquery(
        sql.Select(
            "orders",
            ["1"],
            where=[sql.Compare(sql.Ref("user_id", "orders"), "=", sql.Ref("id", "users"))],
        )
    )
    assert sql.compile_expr(sql.Exists(inner)) == (
        "EXISTS (SELECT 1 FROM orders WHERE orders.user_id = users.id)",
        [],
    )
    assert sql.compile_expr(~sql.Exists(inner)) == (
        "NOT (EXISTS (SELECT 1 FROM orders WHERE orders.user_id = users.id))",
        [],
    )


def test_predicate_subquery_per_dialect() -> None:
    """Переносимость: `$N`/`?`/`%s`; `ANY` под подзапрос не подставляется."""
    inner = sql.Subquery(
        sql.Select("orders", ["user_id"], where=[sql.Compare("total", ">", 100)])
    )
    node = sql.InList("id", inner)
    assert sql.compile_expr(node, sql.POSTGRES) == (
        "id IN (SELECT user_id FROM orders WHERE total > $1)",
        [100],
    )
    assert sql.compile_expr(node, sql.SQLITE) == (
        '"id" IN (SELECT user_id FROM "orders" WHERE "total" > ?)',
        [100],
    )
    assert sql.compile_expr(node, sql.MYSQL) == (
        "`id` IN (SELECT user_id FROM `orders` WHERE `total` > %s)",
        [100],
    )


def test_column_in_accepts_a_subquery() -> None:
    """Публичный путь: `User.c.id.in_(подзапрос)` внутри Query."""
    inner = sql.Subquery(
        sql.Select("orders", ["user_id"], where=[sql.Compare("total", ">", 10)])
    )
    compiled, params = select(User).where(User.c.id.in_(inner)).compile()
    assert compiled == (
        "SELECT id, name, email FROM users"
        " WHERE (id IN (SELECT user_id FROM orders WHERE total > $1))"
    )
    assert params == [10]


def test_derived_table_requires_an_alias() -> None:
    """`FROM (SELECT …)` без алиаса — ValueError; в предикате алиас не печатается."""
    inner = sql.Select("posts", ["id"])
    with pytest.raises(ValueError):
        sql.compile_statement(sql.Select(sql.Subquery(inner), ["id"]))
    assert sql.compile_expr(sql.Exists(sql.Subquery(inner, "t0"))) == (
        "EXISTS (SELECT id FROM posts)",
        [],
    )


# ── множественные операции (UNION/EXCEPT/INTERSECT) ──────────────────────────


def test_union_all_keeps_placeholders_continuous() -> None:
    """Значения обеих ветвей — один список параметров."""
    left = sql.Select("users", ["id"], where=[sql.Compare("age", ">", 30)])
    right = sql.Select("admins", ["id"], where=[sql.Compare("level", ">", 5)])
    statement = sql.SetOp(left, right, all=True)
    assert sql.compile_statement(statement) == (
        "SELECT id FROM users WHERE age > $1 UNION ALL SELECT id FROM admins WHERE level > $2",
        [30, 5],
    )


def test_union_order_and_limit_belong_to_the_compound() -> None:
    statement = sql.SetOp(
        sql.Select("users", ["id"]),
        sql.Select("admins", ["id"]),
        order=["id"],
        limit=10,
        offset=5,
    )
    assert sql.compile_statement(statement) == (
        "SELECT id FROM users UNION SELECT id FROM admins ORDER BY id LIMIT $1 OFFSET $2",
        [10, 5],
    )


def test_intersect_and_except() -> None:
    a = sql.Select("users", ["id"])
    b = sql.Select("bans", ["id"])
    assert sql.compile_statement(sql.SetOp(a, b, kind=sql.INTERSECT)) == (
        "SELECT id FROM users INTERSECT SELECT id FROM bans",
        [],
    )
    assert sql.compile_statement(sql.SetOp(a, b, kind=sql.EXCEPT)) == (
        "SELECT id FROM users EXCEPT SELECT id FROM bans",
        [],
    )


def test_all_variant_is_dialect_dependent() -> None:
    """`UNION ALL` — везде, `EXCEPT ALL`/`INTERSECT ALL` — только PG и MySQL."""
    a = sql.Select("users", ["id"])
    b = sql.Select("bans", ["id"])
    assert sql.compile_statement(sql.SetOp(a, b, all=True), sql.SQLITE) == (
        'SELECT id FROM "users" UNION ALL SELECT id FROM "bans"',
        [],
    )
    except_all = sql.SetOp(a, b, kind=sql.EXCEPT, all=True)
    assert sql.compile_statement(except_all, sql.POSTGRES)[0] == (
        "SELECT id FROM users EXCEPT ALL SELECT id FROM bans"
    )
    assert sql.compile_statement(except_all, sql.MYSQL)[0] == (
        "SELECT id FROM `users` EXCEPT ALL SELECT id FROM `bans`"
    )
    with pytest.raises(sql.DialectError):
        sql.compile_statement(except_all, sql.SQLITE)


def test_operand_with_its_own_limit_is_parenthesised() -> None:
    """Свой LIMIT у ветви — скобки; SQLite скобок не принимает (замерено)."""
    branch = sql.Select("users", ["id"], limit=5)
    statement = sql.SetOp(branch, sql.Select("admins", ["id"]))
    assert sql.compile_statement(statement, sql.POSTGRES) == (
        "(SELECT id FROM users LIMIT $1) UNION SELECT id FROM admins",
        [5],
    )
    assert sql.compile_statement(statement, sql.MYSQL) == (
        "(SELECT id FROM `users` LIMIT %s) UNION SELECT id FROM `admins`",
        [5],
    )
    with pytest.raises(sql.DialectError):
        sql.compile_statement(statement, sql.SQLITE)


def test_nested_set_op_is_parenthesised() -> None:
    first, second, third = (sql.Select(name, ["id"]) for name in ("a", "b", "c"))
    statement = sql.SetOp(sql.SetOp(first, second, kind=sql.EXCEPT), third, kind=sql.INTERSECT)
    assert sql.compile_statement(statement)[0] == (
        "(SELECT id FROM a EXCEPT SELECT id FROM b) INTERSECT SELECT id FROM c"
    )
    with pytest.raises(sql.DialectError):
        sql.compile_statement(statement, sql.SQLITE)


def test_set_op_as_derived_table_and_in_predicate() -> None:
    """Результат — множество строк: годится и в FROM, и в предикат."""
    both = sql.SetOp(sql.Select("users", ["id"]), sql.Select("admins", ["id"]))
    derived = sql.Select(sql.Subquery(both, "t0"), [sql.Ref("id", "t0")])
    assert sql.compile_statement(derived) == (
        "SELECT t0.id FROM (SELECT id FROM users UNION SELECT id FROM admins) t0",
        [],
    )
    predicate = sql.InList("id", sql.Subquery(both))
    assert sql.compile_expr(predicate) == (
        "id IN (SELECT id FROM users UNION SELECT id FROM admins)",
        [],
    )


def test_set_op_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValueError):
        sql.SetOp(sql.Select("users", ["id"]), sql.Select("admins", ["id"]), kind="JOIN")


# ── CTE (WITH) ───────────────────────────────────────────────────────────────


def test_with_keeps_placeholders_continuous() -> None:
    """Значения CTE идут в общий список перед значениями основного запроса."""
    recent = sql.Cte(
        "recent", sql.Select("orders", ["user_id"], where=[sql.Compare("total", ">", 100)])
    )
    statement = sql.With(
        sql.Select("recent", ["user_id"], where=[sql.Compare("user_id", ">", 0)]),
        ctes=[recent],
    )
    assert sql.compile_statement(statement) == (
        "WITH recent AS (SELECT user_id FROM orders WHERE total > $1) "
        "SELECT user_id FROM recent WHERE user_id > $2",
        [100, 0],
    )


def test_with_two_ctes_and_a_join_by_name() -> None:
    """На CTE потом ссылаются как на таблицу — отдельного узла не нужно."""
    first = sql.Cte("a", sql.Select("users", ["id"]))
    second = sql.Cte("b", sql.Select("orders", ["user_id"]))
    statement = sql.With(
        sql.Select(
            "a",
            [sql.Ref("id", "t0")],
            joins=[
                sql.Join(
                    "b",
                    sql.Compare(sql.Ref("user_id", "t1"), "=", sql.Ref("id", "t0")),
                    alias="t1",
                )
            ],
        ),
        ctes=[first, second],
    )
    assert sql.compile_statement(statement) == (
        "WITH a AS (SELECT id FROM users), b AS (SELECT user_id FROM orders) "
        "SELECT t0.id FROM a LEFT JOIN b t1 ON t1.user_id = t0.id",
        [],
    )


def test_with_recursive_per_dialect() -> None:
    counter = sql.Cte("counter", sql.Select("numbers", ["n"], where=[sql.Compare("n", "=", 1)]))
    statement = sql.With(sql.Select("counter", ["n"]), ctes=[counter], recursive=True)
    assert sql.compile_statement(statement, sql.POSTGRES) == (
        "WITH RECURSIVE counter AS (SELECT n FROM numbers WHERE n = $1) SELECT n FROM counter",
        [1],
    )
    assert sql.compile_statement(statement, sql.SQLITE) == (
        'WITH RECURSIVE "counter" AS (SELECT n FROM "numbers" WHERE "n" = ?)'
        ' SELECT n FROM "counter"',
        [1],
    )
    assert sql.compile_statement(statement, sql.MYSQL) == (
        "WITH RECURSIVE `counter` AS (SELECT n FROM `numbers` WHERE `n` = %s)"
        " SELECT n FROM `counter`",
        [1],
    )


def test_with_wraps_a_set_op_and_nests_into_a_subquery() -> None:
    cte = sql.Cte("c", sql.Select("users", ["id"]))
    body = sql.SetOp(sql.Select("c", ["id"]), sql.Select("admins", ["id"]), all=True)
    assert sql.compile_statement(sql.With(body, ctes=[cte]))[0] == (
        "WITH c AS (SELECT id FROM users) SELECT id FROM c UNION ALL SELECT id FROM admins"
    )
    nested = sql.With(sql.Select("c", ["id"]), ctes=[cte])
    derived = sql.Select(sql.Subquery(nested, "t0"), [sql.Ref("id", "t0")])
    assert sql.compile_statement(derived)[0] == (
        "SELECT t0.id FROM (WITH c AS (SELECT id FROM users) SELECT id FROM c) t0"
    )
    assert sql.compile_expr(sql.InList("id", sql.Subquery(nested)))[0] == (
        "id IN (WITH c AS (SELECT id FROM users) SELECT id FROM c)"
    )


def test_with_validates_its_ctes() -> None:
    body = sql.Select("users", ["id"])
    with pytest.raises(ValueError):
        sql.With(body, ctes=[])
    with pytest.raises(ValueError):
        sql.With(body, ctes=[sql.Cte("a", body), sql.Cte("a", body)])
    with pytest.raises(ValueError):
        sql.Cte("", body)


# ── оконные функции ──────────────────────────────────────────────────────────


def test_window_over_partition_and_order() -> None:
    """`Func(...).over(...)` и узел `Over(...)` дают одно дерево."""
    expected = ("row_number() OVER (PARTITION BY user_id ORDER BY placed_at DESC)", [])
    assert sql.compile_expr(
        sql.Over(sql.Func("row_number"), partition_by=["user_id"], order=["placed_at DESC"])
    ) == expected
    assert sql.compile_expr(
        sql.Func("row_number").over(partition_by=["user_id"], order=["placed_at DESC"])
    ) == expected


def test_window_in_a_select_list_keeps_params_continuous() -> None:
    running = sql.Func("sum", "total").over(partition_by=["user_id"], order=["id"])
    statement = sql.Select(
        "orders",
        ["id", running],
        where=[sql.Compare("total", ">", 100)],
        order=["id"],
    )
    assert sql.compile_statement(statement) == (
        "SELECT id, sum(total) OVER (PARTITION BY user_id ORDER BY id) FROM orders"
        " WHERE total > $1 ORDER BY id",
        [100],
    )


def test_window_without_partition_or_order() -> None:
    """Пустое окно — весь результат целиком, но это валидный SQL."""
    assert sql.compile_expr(sql.Func("avg", "total").over()) == ("avg(total) OVER ()", [])


def test_window_partition_by_accepts_nodes() -> None:
    node = sql.Func("sum", "total").over(partition_by=[sql.Func("lower", "name")])
    assert sql.compile_expr(node) == ("sum(total) OVER (PARTITION BY lower(name))", [])


def test_window_per_dialect() -> None:
    node = sql.Func("count", "*").over(partition_by=["user_id"])
    assert sql.compile_expr(node, sql.POSTGRES) == ("count(*) OVER (PARTITION BY user_id)", [])
    assert sql.compile_expr(node, sql.SQLITE) == ('count(*) OVER (PARTITION BY "user_id")', [])
    assert sql.compile_expr(node, sql.MYSQL) == ("count(*) OVER (PARTITION BY `user_id`)", [])


# ── алиасы: источник FROM и выражение ───────────────────────────────────────


def test_from_source_can_be_aliased() -> None:
    statement = sql.Select("users", [sql.Ref("id", "t0")], alias="t0")
    assert sql.compile_statement(statement) == ("SELECT t0.id FROM users t0", [])


def test_self_join_with_source_and_join_aliases() -> None:
    """Само-джойн таблицы: `FROM users t0 LEFT JOIN users t1`."""
    statement = sql.Select(
        "users",
        [sql.Ref("id", "t0")],
        alias="t0",
        joins=[
            sql.Join(
                "users",
                sql.Compare(sql.Ref("manager_id", "t0"), "=", sql.Ref("id", "t1")),
                alias="t1",
            )
        ],
        where=[sql.Compare(sql.Ref("name", "t1"), "=", "Ann")],
    )
    assert sql.compile_statement(statement) == (
        "SELECT t0.id FROM users t0 LEFT JOIN users t1 ON t0.manager_id = t1.id"
        " WHERE t1.name = $1",
        ["Ann"],
    )


def test_source_alias_for_a_derived_table_is_rejected() -> None:
    """Алиас задаётся один раз: у производной таблицы он внутри Subquery."""
    inner = sql.Select("posts", ["id"])
    with pytest.raises(ValueError):
        sql.Select(sql.Subquery(inner, "t0"), ["id"], alias="t0")


def test_as_names_an_output_column() -> None:
    expected = ("sum(total) AS total_sum", [])
    assert sql.compile_expr(sql.As(sql.Func("sum", "total"), "total_sum")) == expected
    assert sql.compile_expr(sql.Func("sum", "total").as_("total_sum")) == expected


def test_as_is_quoted_per_dialect() -> None:
    node = sql.Func("count", "*").as_("total")
    assert sql.compile_expr(node, sql.POSTGRES) == ("count(*) AS total", [])
    assert sql.compile_expr(node, sql.SQLITE) == ('count(*) AS "total"', [])
    assert sql.compile_expr(node, sql.MYSQL) == ("count(*) AS `total`", [])


def test_top_n_per_group_with_nodes_only() -> None:
    """Тот же top-N, что раньше требовал строки с `AS rn`, — теперь узлами."""
    ranked = sql.Select(
        "orders",
        [
            "id",
            "user_id",
            "total",
            sql.Func("row_number")
            .over(partition_by=["user_id"], order=["total DESC"])
            .as_("rn"),
        ],
    )
    statement = sql.Select(
        sql.Subquery(ranked, "t0"),
        [sql.Ref("id", "t0"), sql.Ref("rn", "t0")],
        where=[sql.Compare(sql.Ref("rn", "t0"), "=", 1)],
    )
    assert sql.compile_statement(statement) == (
        "SELECT t0.id, t0.rn FROM (SELECT id, user_id, total,"
        " row_number() OVER (PARTITION BY user_id ORDER BY total DESC) AS rn FROM orders) t0"
        " WHERE t0.rn = $1",
        [1],
    )


def test_as_requires_an_alias() -> None:
    with pytest.raises(ValueError):
        sql.As(sql.Func("sum", "total"), "")


# ── приведения типов (CAST) ──────────────────────────────────────────────────


def test_cast_uses_the_dialect_type_map() -> None:
    """Одно каноническое имя — три разных SQL-типа."""
    node = sql.Func("sum", "total").cast(sql.NUMERIC)
    assert sql.compile_expr(node, sql.POSTGRES) == ("CAST(sum(total) AS numeric)", [])
    assert sql.compile_expr(node, sql.MYSQL) == ("CAST(sum(`total`) AS DECIMAL)", [])
    assert sql.compile_expr(node, sql.SQLITE) == ('CAST(sum("total") AS NUMERIC)', [])


def test_cast_type_map_covers_the_common_targets() -> None:
    def rendered(type_name: str, dialect: sql.Dialect) -> str:
        return sql.compile_expr(sql.Cast(sql.Ref("x"), type_name), dialect)[0]

    assert rendered(sql.INT, sql.POSTGRES) == "CAST(x AS integer)"
    assert rendered(sql.INT, sql.MYSQL) == "CAST(`x` AS SIGNED)"
    assert rendered(sql.TEXT, sql.MYSQL) == "CAST(`x` AS CHAR)"
    assert rendered(sql.REAL, sql.POSTGRES) == "CAST(x AS double precision)"
    assert rendered(sql.BOOLEAN, sql.SQLITE) == 'CAST("x" AS INTEGER)'


def test_cast_to_date_is_refused_where_there_is_no_date_type() -> None:
    """PG/MySQL дату умеют, SQLite — нет (там это молчаливая пустышка)."""
    assert sql.compile_expr(sql.Cast(sql.Ref("placed_at"), sql.DATE), sql.POSTGRES) == (
        "CAST(placed_at AS date)",
        [],
    )
    assert sql.compile_expr(sql.Cast(sql.Ref("placed_at"), sql.TIMESTAMP), sql.MYSQL) == (
        "CAST(`placed_at` AS DATETIME)",
        [],
    )
    with pytest.raises(sql.DialectError):
        sql.compile_expr(sql.Cast(sql.Ref("placed_at"), sql.DATE), sql.SQLITE)
    with pytest.raises(sql.DialectError):
        sql.compile_expr(sql.Cast(sql.Ref("placed_at"), sql.TIMESTAMP), sql.SQLITE)


def test_cast_rejects_an_unknown_type_name() -> None:
    """Только канонические имена: движковый SQL-тип здесь не примут."""
    with pytest.raises(ValueError):
        sql.Cast(sql.Ref("x"), "varchar")


def test_cast_in_a_select_list_with_alias() -> None:
    statement = sql.Select(
        "orders",
        [sql.Func("avg", "total").cast(sql.NUMERIC).as_("avg_total")],
        where=[sql.Compare("user_id", "=", 1)],
    )
    assert sql.compile_statement(statement) == (
        "SELECT CAST(avg(total) AS numeric) AS avg_total FROM orders WHERE user_id = $1",
        [1],
    )


def test_cast_participates_in_a_comparison() -> None:
    node = sql.Compare(sql.Cast(sql.Ref("total"), sql.NUMERIC), ">", 100)
    assert sql.compile_expr(node) == ("CAST(total AS numeric) > $1", [100])


def test_numeric_with_precision_and_scale_is_money() -> None:
    """Главный практический случай: деньги без float."""
    node = sql.Func("sum", "total").cast(sql.NUMERIC, precision=10, scale=2)
    assert sql.compile_expr(node, sql.POSTGRES) == ("CAST(sum(total) AS numeric(10, 2))", [])
    assert sql.compile_expr(node, sql.MYSQL) == ("CAST(sum(`total`) AS DECIMAL(10, 2))", [])
    assert sql.compile_expr(node, sql.SQLITE) == ('CAST(sum("total") AS NUMERIC(10, 2))', [])


def test_precision_without_scale() -> None:
    node = sql.Cast(sql.Ref("total"), sql.NUMERIC, precision=10)
    assert sql.compile_expr(node, sql.POSTGRES) == ("CAST(total AS numeric(10))", [])
    assert sql.compile_expr(node, sql.SQLITE) == ('CAST("total" AS NUMERIC(10))', [])


def test_cast_parameters_are_validated() -> None:
    with pytest.raises(ValueError):
        sql.Cast(sql.Ref("total"), sql.NUMERIC, scale=2)
    with pytest.raises(ValueError):
        sql.Cast(sql.Ref("total"), sql.INT, precision=5)


def test_engine_specific_types_where_they_exist() -> None:
    def rendered(type_name: str, dialect: sql.Dialect) -> str:
        return sql.compile_expr(sql.Cast(sql.Ref("value"), type_name), dialect)[0]

    assert rendered(sql.JSON, sql.POSTGRES) == "CAST(value AS jsonb)"
    assert rendered(sql.JSON, sql.MYSQL) == "CAST(`value` AS JSON)"
    with pytest.raises(sql.DialectError):
        sql.compile_expr(sql.Cast(sql.Ref("value"), sql.JSON), sql.SQLITE)

    assert rendered(sql.UUID, sql.POSTGRES) == "CAST(value AS uuid)"
    with pytest.raises(sql.DialectError):
        sql.compile_expr(sql.Cast(sql.Ref("value"), sql.UUID), sql.MYSQL)
    with pytest.raises(sql.DialectError):
        sql.compile_expr(sql.Cast(sql.Ref("value"), sql.UUID), sql.SQLITE)

    assert rendered(sql.TIME, sql.POSTGRES) == "CAST(value AS time)"
    assert rendered(sql.TIME, sql.MYSQL) == "CAST(`value` AS TIME)"
    with pytest.raises(sql.DialectError):
        sql.compile_expr(sql.Cast(sql.Ref("value"), sql.TIME), sql.SQLITE)

    assert rendered(sql.BYTES, sql.POSTGRES) == "CAST(value AS bytea)"
    assert rendered(sql.BYTES, sql.MYSQL) == "CAST(`value` AS BINARY)"
    assert rendered(sql.BYTES, sql.SQLITE) == 'CAST("value" AS BLOB)'


def test_no_cast_type_is_silently_dropped() -> None:
    """Каждый канонический тип либо получает SQL-имя, либо отказ — третьего нет."""
    for dialect in (sql.POSTGRES, sql.MYSQL, sql.SQLITE):
        for type_name in sql.CAST_TYPES:
            try:
                spelled = dialect.cast_type(type_name)
            except sql.DialectError:
                continue
            assert spelled, f"{dialect.name}: {type_name} отрендерился пустым"


# ── RETURNING у DELETE ───────────────────────────────────────────────────────


def test_delete_without_returning_is_unchanged() -> None:
    assert sql.compile_statement(sql.Delete("users", "id"))[0] == (
        "DELETE FROM users WHERE id = $1"
    )


def test_multi_row_insert_renders_one_group_per_row() -> None:
    """Многострочный INSERT — то, на чём стоит батч: один статьмент на чанк."""
    statement = sql.Insert("users", ("name", "age"), rows=3)
    assert sql.compile_statement(statement) == (
        "INSERT INTO users (name, age) VALUES ($1, $2), ($3, $4), ($5, $6)",
        [],
    )
    assert sql.compile_statement(statement, sql.MYSQL)[0] == (
        "INSERT INTO `users` (name, age) VALUES (%s, %s), (%s, %s), (%s, %s)"
    )


def test_parameter_limit_is_per_dialect() -> None:
    """Из лимита плейсхолдеров считается размер чанка батча."""
    assert sql.POSTGRES.max_parameters == 32_767
    assert sql.MYSQL.max_parameters == 65_535
    assert sql.SQLITE.max_parameters == 32_766


def test_delete_returning_per_dialect() -> None:
    """`DELETE … RETURNING` есть у PG и SQLite, у MySQL — отказ."""
    statement = sql.Delete("users", "id", returning=("id", "name"))
    assert sql.compile_statement(statement, sql.POSTGRES) == (
        "DELETE FROM users WHERE id = $1 RETURNING id, name",
        [],
    )
    assert sql.compile_statement(statement, sql.SQLITE)[0] == (
        'DELETE FROM "users" WHERE "id" = ? RETURNING id, name'
    )
    with pytest.raises(sql.DialectError):
        sql.compile_statement(statement, sql.MYSQL)


# ── DDL (миграции) ───────────────────────────────────────────────────────────


def test_create_table_renders_per_dialect() -> None:
    """Один и тот же `CreateTable` — три разных CREATE TABLE."""
    statement = sql.CreateTable(
        "orders",
        [
            sql.Column("id", sql.INT, pk=True),
            sql.Column("total", sql.NUMERIC, nullable=False, precision=12, scale=2),
            sql.Column("status", sql.TEXT, default="new"),
        ],
    )
    assert sql.compile_statement(statement, sql.POSTGRES) == (
        "CREATE TABLE IF NOT EXISTS orders (id integer PRIMARY KEY GENERATED BY DEFAULT AS"
        " IDENTITY, total numeric(12, 2) NOT NULL, status text DEFAULT 'new')",
        [],
    )
    assert sql.compile_statement(statement, sql.MYSQL) == (
        "CREATE TABLE IF NOT EXISTS `orders` (`id` int PRIMARY KEY AUTO_INCREMENT,"
        " `total` decimal(12, 2) NOT NULL, `status` varchar(255) DEFAULT 'new')",
        [],
    )
    assert sql.compile_statement(statement, sql.SQLITE) == (
        'CREATE TABLE IF NOT EXISTS "orders" ("id" INTEGER PRIMARY KEY AUTOINCREMENT,'
        ' "total" NUMERIC(12, 2) NOT NULL, "status" TEXT DEFAULT \'new\')',
        [],
    )


def test_column_default_is_escaped_and_raw_goes_as_written() -> None:
    """В DDL нет bound-значений: строка экранируется, Raw уходит как написан."""
    quoted = sql.CreateTable("people", [sql.Column("name", sql.TEXT, default="O'Brien")])
    assert "DEFAULT 'O''Brien'" in sql.compile_statement(quoted)[0]

    stamp = sql.Column("placed_at", sql.TIMESTAMP, default=sql.Raw("CURRENT_TIMESTAMP"))
    with_stamp = sql.CreateTable("orders", [sql.Column("id", sql.INT, pk=True), stamp])
    assert "DEFAULT CURRENT_TIMESTAMP" in sql.compile_statement(with_stamp)[0]

    with pytest.raises(ValueError):
        sql.Column("x", sql.TEXT, default=sql.Raw("$1", ["a"]))


def test_add_column_and_drop_table() -> None:
    assert sql.compile_statement(sql.AddColumn("orders", sql.Column("note", sql.TEXT)))[0] == (
        "ALTER TABLE orders ADD COLUMN note text"
    )
    assert sql.compile_statement(sql.DropTable("orders"))[0] == "DROP TABLE IF EXISTS orders"
    assert sql.compile_statement(sql.DropTable("orders", if_exists=False))[0] == "DROP TABLE orders"


def test_index_if_not_exists_is_dialect_dependent() -> None:
    """`IF NOT EXISTS` у индекса — только там, где движок это умеет."""
    plain = sql.CreateIndex("ix", "orders", ["user_id"])
    assert sql.compile_statement(plain, sql.MYSQL)[0] == (
        "CREATE INDEX `ix` ON `orders` (`user_id`)"
    )
    marked = sql.CreateIndex("ix", "orders", ["user_id"], unique=True, if_not_exists=True)
    assert sql.compile_statement(marked, sql.POSTGRES)[0] == (
        "CREATE UNIQUE INDEX IF NOT EXISTS ix ON orders (user_id)"
    )
    with pytest.raises(sql.DialectError):
        sql.compile_statement(marked, sql.MYSQL)


def test_column_types_differ_from_cast_types() -> None:
    """Роли разные: `CAST(x AS SIGNED)` — не объявление колонки `int`."""
    assert sql.MYSQL.cast_types[sql.INT] == "SIGNED"
    assert sql.MYSQL.column_type(sql.INT) == "int"
    assert sql.MYSQL.column_type(sql.TEXT) == "varchar(255)"
    assert sql.MYSQL.column_type(sql.UUID) == "char(36)"
    assert sql.SQLITE.column_type(sql.TIMESTAMP) == "TEXT"
    assert sql.SQLITE.column_type(sql.JSON) == "TEXT"


def test_ddl_validates_its_input() -> None:
    with pytest.raises(ValueError):
        sql.Column("", sql.INT)
    with pytest.raises(ValueError):
        sql.Column("x", "varchar")
    with pytest.raises(ValueError):
        sql.CreateTable("t", [])
    with pytest.raises(ValueError):
        sql.CreateTable("t", [sql.Column("a", sql.INT), sql.Column("a", sql.INT)])
    with pytest.raises(ValueError):
        sql.CreateIndex("ix", "t", [])


def test_the_example_migration_compiles_for_every_dialect() -> None:
    """Файл-пример из examples/migrations собирается под все три движка без значений."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "examples/migrations/0001_orders.py"
    spec = importlib.util.spec_from_file_location("example_migration_0001", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["example_migration_0001"] = module
    spec.loader.exec_module(module)

    for dialect in (sql.POSTGRES, sql.MYSQL, sql.SQLITE):
        for step in (*module.UP, *module.DOWN):
            text, params = sql.compile_statement(step, dialect)
            assert text, f"{dialect.name}: empty step"
            assert params == [], f"{dialect.name}: DDL must carry no values"
