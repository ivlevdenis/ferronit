"""SQL expression tree and dialects — the generator behind the raw data layer.

The data layer used to build SQL with f-strings: fast (compilation is ~0.14 % of
a query's cost) but tied to one engine — placeholder style, ``ANY`` vs ``IN``,
``EXCLUDED`` casing and ``RETURNING`` support differ between databases. This
module keeps the PostgreSQL strings byte-identical while making the generator
dialect-aware:

    node = Compare("age", ">", 30)
    compile_expr(node, POSTGRES)   # ("age > $1", [30])
    compile_expr(node, SQLITE)     # ("age > ?", [30])

Expressions are cheap tree nodes: ``&``/``|``/``~`` combine them, ``And``/``Or``
are binary (matching the historical parenthesisation), and rendering happens once
in :func:`compile_expr` / :func:`compile_statement`. Values always travel as bound
parameters — never interpolated into the SQL text.

Escape hatch: :class:`Raw` (and anything exposing ``sql``/``params``, e.g.
``rawdb.Condition``) is rendered as hand-written SQL, with its ``$N`` placeholders
renumbered to the dialect's style.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

__all__ = [
    "FULL",
    "INNER",
    "JOIN_KINDS",
    "LEFT",
    "MYSQL",
    "POSTGRES",
    "RIGHT",
    "SQLITE",
    "And",
    "Between",
    "Compare",
    "Delete",
    "Dialect",
    "DialectError",
    "Expr",
    "Func",
    "InList",
    "Insert",
    "IsNull",
    "Join",
    "Like",
    "MySQLDialect",
    "Not",
    "Or",
    "Param",
    "Paren",
    "PostgresDialect",
    "Raw",
    "Ref",
    "SQLiteDialect",
    "Select",
    "Subquery",
    "Update",
    "compile_expr",
    "compile_statement",
    "renumber_placeholders",
]

_PLACEHOLDER_RE = re.compile(r"\$(\d+)")


class DialectError(Exception):
    """Raised when a statement needs a feature the dialect cannot express."""


class Dialect:
    """Rendering rules for one SQL engine.

    The flags describe optional features; a statement is always built the same
    way and rejected loudly (:class:`DialectError`) when the target engine
    cannot express it, instead of silently emitting wrong SQL.

    Attributes:
        name: Short engine name, used in messages and tests.
        supports_any: Engine understands ``col = ANY($1)`` for list binds.
        supports_ilike: Engine understands ``ILIKE``.
        supports_returning: Engine supports ``RETURNING``.
        supports_upsert: Engine supports ``INSERT … ON CONFLICT DO UPDATE``.
        supports_placeholder_reuse: One parameter may be referenced twice
            (``$1 … $1``) instead of repeating the placeholder.
        supports_full_join: Engine understands ``FULL JOIN`` (MySQL has no
            ``FULL OUTER JOIN``; PostgreSQL and SQLite 3.39+ do).
        excluded_keyword: Keyword for the conflicting row in an upsert.
    """

    name = "generic"
    supports_any = False
    supports_ilike = False
    supports_returning = False
    supports_upsert = False
    supports_placeholder_reuse = False
    supports_full_join = False

    def placeholder(self, index: int) -> str:
        """Render the placeholder of one bound parameter.

        Args:
            index: 1-based parameter position.

        Returns:
            The engine's placeholder text.
        """
        return "?"

    def quote(self, name: str) -> str:
        """Quote an identifier.

        Args:
            name: Column or table name (comes from a model, never from input).

        Returns:
            The identifier as written in SQL; dialects that need quoting
            override this.
        """
        return name

    def conflict_clause(self, key: str, update_columns: Sequence[str]) -> str:
        """Render the upsert tail of an ``INSERT`` (conflict handling).

        Rendering lives on the dialect because the syntax is engine-specific:
        PostgreSQL has ``ON CONFLICT (key) DO UPDATE SET …``, MySQL/MariaDB have
        ``ON DUPLICATE KEY UPDATE …``. The base dialect has no upsert at all.

        Args:
            key: Conflict target column.
            update_columns: Columns refreshed from the conflicting row.

        Returns:
            The SQL fragment, starting with a space.

        Raises:
            DialectError: If the engine has no upsert.
        """
        raise DialectError(f"{self.name}: upsert is not supported")


class PostgresDialect(Dialect):
    """PostgreSQL: ``$N`` placeholders, ``ANY``/``ILIKE``/``RETURNING``/upsert."""

    name = "postgres"
    supports_any = True
    supports_ilike = True
    supports_returning = True
    supports_upsert = True
    supports_placeholder_reuse = True
    supports_full_join = True

    def placeholder(self, index: int) -> str:
        """Render ``$index``.

        Args:
            index: 1-based parameter position.

        Returns:
            The PostgreSQL placeholder, e.g. ``$3``.
        """
        return f"${index}"

    def conflict_clause(self, key: str, update_columns: Sequence[str]) -> str:
        """Render ``ON CONFLICT (key) DO UPDATE SET c = EXCLUDED.c``.

        Args:
            key: Conflict target column.
            update_columns: Columns refreshed from the conflicting row.

        Returns:
            The ``ON CONFLICT`` fragment.
        """
        assignments = ", ".join(
            f"{self.quote(c)} = EXCLUDED.{self.quote(c)}" for c in update_columns
        )
        return f" ON CONFLICT ({self.quote(key)}) DO UPDATE SET {assignments}"


class MySQLDialect(Dialect):
    """MySQL / MariaDB: ``%s`` placeholders, backticked identifiers, ``REPLACE``-style upsert.

    Engine differences that matter for generated SQL:

    * no ``col = ANY($n)`` — list filters become ``IN (%s, %s, …)``;
    * no ``ILIKE`` — ``LIKE`` is case-insensitive under the default collation,
      so an insensitive match falls back to ``LIKE``;
    * no ``RETURNING`` on ``INSERT``/``UPDATE``/``DELETE`` — a statement asking
      for it raises :class:`DialectError` (the generated key has to come from the
      driver, e.g. ``cursor.lastrowid``);
    * upsert is ``ON DUPLICATE KEY UPDATE``, not ``ON CONFLICT``.

    Note:
        These drivers use ``%``-style parameter interpolation, so a literal
        percent sign inside a hand-written :class:`Raw` fragment must be written
        ``%%`` — bound values are unaffected.
    """

    name = "mysql"
    supports_upsert = True

    def placeholder(self, index: int) -> str:
        """Render ``%s`` (the position is implied by the order of values).

        Args:
            index: 1-based parameter position.

        Returns:
            The literal ``%s``.
        """
        return "%s"

    def quote(self, name: str) -> str:
        """Quote an identifier with backticks.

        Args:
            name: Column or table name.

        Returns:
            The identifier in backticks (``\\`users\\```), as MySQL expects.
        """
        return f"`{name}`"

    def conflict_clause(self, key: str, update_columns: Sequence[str]) -> str:
        """Render ``ON DUPLICATE KEY UPDATE c = VALUES(c)``.

        ``VALUES(col)`` is deprecated since MySQL 8.0.20 in favour of the
        ``AS new`` alias form, but the alias is not available on MariaDB — the
        deprecated form is the portable one and still works on both.

        Args:
            key: Ignored — MySQL keys off any unique index, not a named target.
            update_columns: Columns refreshed from the conflicting row.

        Returns:
            The ``ON DUPLICATE KEY UPDATE`` fragment.
        """
        assignments = ", ".join(
            f"{self.quote(c)} = VALUES({self.quote(c)})" for c in update_columns
        )
        return f" ON DUPLICATE KEY UPDATE {assignments}"


class SQLiteDialect(Dialect):
    """SQLite: ``?`` placeholders, double-quoted identifiers, ``RETURNING`` (3.35+) and upsert.

    SQLite is a library, not a server: it needs no container, and the same SQL can
    run against a file or ``:memory:``.
    """

    name = "sqlite"
    supports_returning = True
    supports_upsert = True
    supports_full_join = True

    def quote(self, name: str) -> str:
        """Quote an identifier with double quotes.

        Args:
            name: Column or table name.

        Returns:
            The identifier in double quotes (``"users"``), as SQLite expects.
        """
        return f'"{name}"'

    def conflict_clause(self, key: str, update_columns: Sequence[str]) -> str:
        """Render ``ON CONFLICT (key) DO UPDATE SET c = excluded.c``.

        Args:
            key: Conflict target column.
            update_columns: Columns refreshed from the conflicting row.

        Returns:
            The ``ON CONFLICT`` fragment.
        """
        assignments = ", ".join(
            f"{self.quote(c)} = excluded.{self.quote(c)}" for c in update_columns
        )
        return f" ON CONFLICT ({self.quote(key)}) DO UPDATE SET {assignments}"


POSTGRES = PostgresDialect()
SQLITE = SQLiteDialect()
MYSQL = MySQLDialect()


def renumber_placeholders(sql: str, offset: int, dialect: Dialect = POSTGRES) -> str:
    """Shift every ``$N`` placeholder in a fragment by ``offset``.

    Used when a hand-written fragment is embedded into a larger statement: its
    values are appended after the earlier ones, so its placeholders move by the
    number of parameters already bound. Renumbering works from the text, not by
    counting, so ``$1`` never eats the start of ``$10``.

    Args:
        sql: SQL fragment with ``$N`` placeholders.
        offset: Number of parameters already bound before this fragment.
        dialect: Dialect whose placeholder style the fragment is rewritten into.

    Returns:
        The fragment with shifted placeholders.

    Example:
        ``renumber_placeholders("b = $1", 2)`` returns ``"b = $3"``.
    """
    if offset == 0 and isinstance(dialect, PostgresDialect):
        return sql
    return _PLACEHOLDER_RE.sub(
        lambda match: dialect.placeholder(int(match.group(1)) + offset), sql
    )


class Expr:
    """Base class for SQL expression nodes.

    ``&``, ``|`` and ``~`` combine nodes into a tree; nothing is rendered until
    :func:`compile_expr` runs, so the same tree can be compiled for several
    dialects.
    """

    __slots__ = ()

    def __and__(self, other: Expr) -> And:
        """Combine with ``AND`` (``a & b``)."""
        return And(self, other)

    def __or__(self, other: Expr) -> Or:
        """Combine with ``OR`` (``a | b``)."""
        return Or(self, other)

    def __invert__(self) -> Not:
        """Negate (``~a`` → ``NOT (a)``)."""
        return Not(self)


class Raw(Expr):
    """A hand-written SQL fragment with ``$N`` placeholders.

    Placeholders are renumbered on render, so the fragment composes with other
    nodes while its values stay bound. Use it for expressions the tree does not
    model (``count(*)``, functions, engine-specific SQL).

    Args:
        sql: SQL text with ``$1``-style placeholders.
        params: Values for those placeholders, in order.
    """

    __slots__ = ("params", "sql")

    def __init__(self, sql: str, params: Sequence[Any] = ()) -> None:
        self.sql = sql
        self.params = list(params)


class Param(Expr):
    """A single bound value.

    Args:
        value: The value to bind; it is never interpolated into SQL.
    """

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value


class Ref(Expr):
    """A column reference, optionally qualified by a table alias (``t0.id``).

    Needed once a query joins tables: the same column name appears in several of
    them, so each reference has to name its table. The column is quoted per
    dialect; the alias is written as given (aliases are generated by the caller:
    ``t0`` for the outer query, ``t1``/``t2``/… for its joins).

    Args:
        column: Column name.
        alias: Table alias to qualify with; ``None`` renders the bare column.
    """

    __slots__ = ("alias", "column")

    def __init__(self, column: str, alias: str | None = None) -> None:
        self.column = column
        self.alias = alias


class Compare(Expr):
    """Comparison of a value-producing left side with another value (``age > $1``).

    ``= None`` / ``!= None`` render as ``IS NULL`` / ``IS NOT NULL``.

    Args:
        column: Column name, or any expression (e.g. ``Func("count")``) — that is
            how aggregates appear in ``HAVING``.
        op: One of ``= != <> < <= > >=``.
        value: Bound value, ``None`` for a NULL check, or an expression — a node
            such as :class:`Ref` is rendered as SQL instead of being bound, which
            is how column-to-column conditions (``ON t1.user_id = t0.id``) are
            written.
    """

    __slots__ = ("column", "op", "value")

    def __init__(self, column: str | Expr, op: str, value: Any) -> None:
        self.column = column
        self.op = op
        self.value = value


class Func(Expr):
    """A function call — ``count(*)``, ``sum(age)``, ``max(id)``.

    Column arguments are quoted per dialect; ``"*"`` and anything containing a
    parenthesis is written as is (that is how ``count(*)`` and nested calls are
    expressed). Values are never interpolated: a function argument that must be a
    bound parameter has to go through :class:`Compare`.

    Args:
        name: Function name (``count``, ``sum``, …).
        *args: Column names, ``"*"``, or expression nodes.
    """

    __slots__ = ("args", "name")

    def __init__(self, name: str, *args: Any) -> None:
        self.name = name
        self.args = list(args)


class IsNull(Expr):
    """``column IS NULL`` / ``column IS NOT NULL``.

    Args:
        column: Column name.
        negated: Render ``IS NOT NULL`` when true.
    """

    __slots__ = ("column", "negated")

    def __init__(self, column: str, *, negated: bool = False) -> None:
        self.column = column
        self.negated = negated


class InList(Expr):
    """``column = ANY($1)`` on PostgreSQL, ``column IN (…)`` elsewhere.

    Args:
        column: Column name.
        values: The candidate values, bound as one list parameter on engines
            that support ``ANY``.
        negated: Wrap the predicate in ``NOT (…)``.
    """

    __slots__ = ("column", "negated", "values")

    def __init__(self, column: str, values: Sequence[Any], *, negated: bool = False) -> None:
        self.column = column
        self.values = list(values)
        self.negated = negated


class Like(Expr):
    """``LIKE``/``ILIKE`` against a bound pattern.

    Args:
        column: Column name.
        pattern: Pattern with ``%``/``_`` wildcards.
        insensitive: Use ``ILIKE`` when the dialect supports it, else ``LIKE``.
        negated: Wrap the predicate in ``NOT (…)``.
    """

    __slots__ = ("column", "insensitive", "negated", "pattern")

    def __init__(
        self,
        column: str,
        pattern: str,
        *,
        insensitive: bool = False,
        negated: bool = False,
    ) -> None:
        self.column = column
        self.pattern = pattern
        self.insensitive = insensitive
        self.negated = negated


class Between(Expr):
    """``column BETWEEN $1 AND $2``.

    Args:
        column: Column name.
        low: Lower bound (bound parameter).
        high: Upper bound (bound parameter).
    """

    __slots__ = ("column", "high", "low")

    def __init__(self, column: str, low: Any, high: Any) -> None:
        self.column = column
        self.low = low
        self.high = high


class And(Expr):
    """Binary ``AND``: renders as ``(left) AND (right)``.

    Binary (not n-ary) on purpose: it keeps the parenthesisation of the original
    string builder, where ``a & b & c`` nests left and ``where(a, b, c)`` joins
    flat.

    Args:
        left: Left operand.
        right: Right operand.
    """

    __slots__ = ("left", "right")

    def __init__(self, left: Any, right: Any) -> None:
        self.left = left
        self.right = right


class Or(Expr):
    """Binary ``OR``: renders as ``(left) OR (right)``.

    Args:
        left: Left operand.
        right: Right operand.
    """

    __slots__ = ("left", "right")

    def __init__(self, left: Any, right: Any) -> None:
        self.left = left
        self.right = right


class Not(Expr):
    """``NOT (operand)``.

    Args:
        operand: Expression to negate.
    """

    __slots__ = ("operand",)

    def __init__(self, operand: Any) -> None:
        self.operand = operand


class Paren(Expr):
    """Wrap an expression in parentheses.

    The data layer's historical output parenthesises every ``WHERE`` item of a
    ``Query``/``_merge_filters`` (``WHERE (age > $1)``) while a simple key lookup
    stays bare (``WHERE id = $1``). ``Paren`` keeps both shapes expressible in the
    tree instead of in string concatenation.

    Args:
        operand: Expression to wrap.
    """

    __slots__ = ("operand",)

    def __init__(self, operand: Any) -> None:
        self.operand = operand


# ── Rendering ─────────────────────────────────────────────────────────────────


def _bind(params: list[Any], dialect: Dialect, value: Any, offset: int = 0) -> str:
    """Append one bound value and return its placeholder."""
    params.append(value)
    return dialect.placeholder(offset + len(params))


def _render_raw(
    sql: str, values: Sequence[Any], dialect: Dialect, params: list[Any], offset: int
) -> str:
    """Renumber a ``$N`` fragment to the dialect, binding its values in place.

    A fragment with no values is *shape-only*: it comes from a precompiled
    statement (``WHERE pk = $1``) whose values are supplied at execution time, so
    placeholders are rendered left to right and nothing is bound.
    """
    base = offset + len(params)
    if not values:
        counter = 0

        def shape(match: re.Match[str]) -> str:
            nonlocal counter
            counter += 1
            return dialect.placeholder(base + counter)

        return _PLACEHOLDER_RE.sub(shape, sql)
    if dialect.supports_placeholder_reuse:
        params.extend(values)
        return _PLACEHOLDER_RE.sub(
            lambda match: dialect.placeholder(int(match.group(1)) + base), sql
        )
    # Диалекты без повторного использования плейсхолдеров ($1 дважды): каждая
    # позиция — отдельный параметр, значение дублируется на вхождение. Иначе
    # параметры съезжают относительно SQL.
    pieces: list[str] = []
    last = 0
    for match in _PLACEHOLDER_RE.finditer(sql):
        pieces.append(sql[last : match.start()])
        params.append(values[int(match.group(1)) - 1])
        pieces.append(dialect.placeholder(offset + len(params)))
        last = match.end()
    pieces.append(sql[last:])
    return "".join(pieces)


def compile_expr(node: Any, dialect: Dialect = POSTGRES, offset: int = 0) -> tuple[str, list[Any]]:
    """Render one expression node to ``(sql, params)``.

    Args:
        node: An :class:`Expr` subclass, or any object exposing ``sql`` and
            ``params`` (hand-written fragment, e.g. ``rawdb.Condition``).
        dialect: Target dialect; PostgreSQL by default.
        offset: Number of parameters already bound before this node, so a
            fragment joined onto an earlier one keeps continuous placeholders.

    Returns:
        The SQL fragment and its bound values, in placeholder order.

    Raises:
        DialectError: If the node needs a feature the dialect lacks.
        TypeError: If the node is not an expression.
    """
    params: list[Any] = []
    return _render(node, dialect, params, offset), params


def _render(
    node: Any, dialect: Dialect, params: list[Any], offset: int = 0
) -> str:
    """Render ``node`` into SQL text, appending values to ``params``."""
    if isinstance(node, Raw):
        return _render_raw(node.sql, node.params, dialect, params, offset)
    if not isinstance(node, Expr):
        sql = getattr(node, "sql", None)
        values = getattr(node, "params", None)
        if isinstance(sql, str) and values is not None:
            return _render_raw(sql, list(values), dialect, params, offset)
        raise TypeError(f"not a SQL expression: {node!r}")
    if isinstance(node, Param):
        return _bind(params, dialect, node.value, offset)
    if isinstance(node, Ref):
        column = dialect.quote(node.column)
        return column if node.alias is None else f"{node.alias}.{column}"
    if isinstance(node, Compare):
        left = (
            dialect.quote(node.column)
            if isinstance(node.column, str)
            else _render(node.column, dialect, params, offset)
        )
        if node.value is None:
            if node.op == "!=":
                return f"{left} IS NOT NULL"
            return f"{left} IS NULL"
        op = "<>" if node.op == "!=" else node.op
        # Значение-узел (Ref) — это SQL, а не bound-параметр: так пишется
        # сравнение колонки с колонкой в ON.
        right = (
            _render(node.value, dialect, params, offset)
            if isinstance(node.value, Expr)
            else _bind(params, dialect, node.value, offset)
        )
        return f"{left} {op} {right}"
    if isinstance(node, Func):
        name = node.name
        rendered = []
        for arg in node.args:
            if isinstance(arg, str) and arg != "*" and "(" not in arg:
                rendered.append(dialect.quote(arg))
            elif isinstance(arg, str):
                rendered.append(arg)
            else:
                rendered.append(_render(arg, dialect, params, offset))
        return f"{name}({', '.join(rendered)})"
    if isinstance(node, IsNull):
        return f"{dialect.quote(node.column)} IS {'NOT ' if node.negated else ''}NULL"
    if isinstance(node, InList):
        column = dialect.quote(node.column)
        if dialect.supports_any:
            inner = f"{column} = ANY({_bind(params, dialect, node.values, offset)})"
        elif not node.values:
            inner = f"{column} IN (NULL)"
        else:
            marks = ", ".join(_bind(params, dialect, value, offset) for value in node.values)
            inner = f"{column} IN ({marks})"
        return f"NOT ({inner})" if node.negated else inner
    if isinstance(node, Like):
        column = dialect.quote(node.column)
        keyword = "ILIKE" if node.insensitive and dialect.supports_ilike else "LIKE"
        inner = f"{column} {keyword} {_bind(params, dialect, node.pattern, offset)}"
        return f"NOT ({inner})" if node.negated else inner
    if isinstance(node, Between):
        column = dialect.quote(node.column)
        low = _bind(params, dialect, node.low, offset)
        high = _bind(params, dialect, node.high, offset)
        return f"{column} BETWEEN {low} AND {high}"
    if isinstance(node, And):
        left = _render(node.left, dialect, params, offset)
        right = _render(node.right, dialect, params, offset)
        return f"({left}) AND ({right})"
    if isinstance(node, Or):
        left = _render(node.left, dialect, params, offset)
        right = _render(node.right, dialect, params, offset)
        return f"({left}) OR ({right})"
    if isinstance(node, Not):
        return f"NOT ({_render(node.operand, dialect, params, offset)})"
    if isinstance(node, Paren):
        return f"({_render(node.operand, dialect, params, offset)})"
    raise TypeError(f"unsupported node: {node!r}")


# ── Statements ────────────────────────────────────────────────────────────────


LEFT = "LEFT"
INNER = "INNER"
RIGHT = "RIGHT"
FULL = "FULL"

JOIN_KINDS = (LEFT, INNER, RIGHT, FULL)


class Subquery:
    """A ``SELECT`` used as a ``FROM`` source — a derived table ``(…) alias``.

    The parenthesised source is what makes a join over a filtered, paginated
    query unambiguous: the inner statement keeps its own ``WHERE``/``ORDER
    BY``/``LIMIT``, while the outer one refers to its columns through ``alias``
    (:class:`Ref`).

    Args:
        statement: The inner statement (a :class:`Select`).
        alias: Alias for the derived table; ``t0`` by convention in the data layer.
    """

    __slots__ = ("alias", "statement")

    def __init__(self, statement: Any, alias: str) -> None:
        self.statement = statement
        self.alias = alias


class Join:
    """One ``JOIN`` clause of a :class:`Select`.

    Rendered as ``<kind> JOIN <table> <alias> ON <condition>``; the joined
    columns are referenced through ``alias`` (:class:`Ref`). ``FULL`` on an
    engine that has no ``FULL OUTER JOIN`` (MySQL) is rejected loudly instead of
    emitting SQL the server refuses — see :attr:`Dialect.supports_full_join`.

    Args:
        table: Joined table name, or a :class:`Subquery` for a derived table.
        on: Join condition — usually a :class:`Compare` of two :class:`Ref`-s; a
            join without it would be invalid SQL, so ``None`` is refused.
        kind: One of :data:`JOIN_KINDS`: ``LEFT`` (default), ``INNER``,
            ``RIGHT``, ``FULL``.
        alias: Table alias for a plain table name (``t1``/``t2``/… by
            convention). A :class:`Subquery` carries its own alias, so passing
            both is refused rather than rendering the alias twice.
    """

    __slots__ = ("alias", "kind", "on", "table")

    def __init__(
        self, table: str | Subquery, on: Any, *, kind: str = LEFT, alias: str | None = None
    ) -> None:
        if kind not in JOIN_KINDS:
            raise ValueError(f"unknown join kind: {kind!r}")
        if on is None:
            raise ValueError("join needs an ON condition")
        if isinstance(table, Subquery):
            if alias is not None:
                raise ValueError("derived table: alias belongs to Subquery")
        elif alias is None:
            raise ValueError("join needs an alias")
        self.table = table
        self.on = on
        self.kind = kind
        self.alias = alias


class Select:
    """``SELECT`` over one table, optionally joined to others.

    Args:
        table: Table name, or a :class:`Subquery` to select from a derived table.
        columns: Select list — column names (written as given), ``count(*)``-style
            fragments, or expression nodes such as :class:`Func`/``Ref``.
        where: Expressions combined with ``AND``; each renders as given, so wrap
            one in :class:`Paren` when the parenthesised shape is wanted.
        distinct: Emit ``SELECT DISTINCT``.
        group_by: ``GROUP BY`` terms — column names (as given) or expressions.
        having: ``HAVING`` expressions combined with ``AND``, like ``where``.
        order: ``ORDER BY`` terms, already rendered (``name``, ``id DESC``).
        limit: Row limit; bound as a parameter when set.
        offset: Row offset; bound as a parameter when set.
        joins: :class:`Join` clauses, rendered between ``FROM`` and ``WHERE``.
    """

    __slots__ = (
        "columns",
        "distinct",
        "group_by",
        "having",
        "joins",
        "limit",
        "offset",
        "order",
        "table",
        "where",
    )

    def __init__(
        self,
        table: str | Subquery,
        columns: Sequence[str | Expr],
        *,
        where: Sequence[Any] = (),
        distinct: bool = False,
        group_by: Sequence[str | Expr] = (),
        having: Sequence[Any] = (),
        order: Sequence[str] = (),
        limit: int | None = None,
        offset: int | None = None,
        joins: Sequence[Join] = (),
    ) -> None:
        self.table = table
        self.columns = list(columns)
        self.where = list(where)
        self.distinct = distinct
        self.group_by = list(group_by)
        self.having = list(having)
        self.order = list(order)
        self.limit = limit
        self.offset = offset
        self.joins = list(joins)


class Insert:
    """``INSERT`` of one or many rows, optionally an upsert / ``RETURNING``.

    Args:
        table: Table name.
        columns: Inserted columns, in value order.
        returning: Columns of the ``RETURNING`` clause (empty = none).
        rows: Number of value rows (``save_many`` uses one statement with many).
        conflict_key: Conflict target for an upsert (``ON CONFLICT (key)``).
        update_columns: Columns refreshed from the conflicting row; defaults to
            every inserted column except ``conflict_key``.
    """

    __slots__ = ("columns", "conflict_key", "returning", "rows", "table", "update_columns")

    def __init__(
        self,
        table: str,
        columns: Sequence[str],
        *,
        returning: Sequence[str] = (),
        rows: int = 1,
        conflict_key: str | None = None,
        update_columns: Sequence[str] | None = None,
    ) -> None:
        self.table = table
        self.columns = list(columns)
        self.returning = list(returning)
        self.rows = rows
        self.conflict_key = conflict_key
        if update_columns is None:
            update_columns = [c for c in self.columns if c != conflict_key]
        self.update_columns = list(update_columns)


class Update:
    """``UPDATE`` of one row by a key column, optionally ``RETURNING``.

    Args:
        table: Table name.
        columns: Columns to set, in value order.
        key: Key column of the ``WHERE`` clause.
        returning: Columns of the ``RETURNING`` clause (empty = none).
    """

    __slots__ = ("columns", "key", "returning", "table")

    def __init__(
        self,
        table: str,
        columns: Sequence[str],
        key: str,
        *,
        returning: Sequence[str] = (),
    ) -> None:
        self.table = table
        self.columns = list(columns)
        self.key = key
        self.returning = list(returning)


class Delete:
    """``DELETE`` by one key column.

    Args:
        table: Table name.
        key: Key column of the ``WHERE`` clause.
    """

    __slots__ = ("key", "table")

    def __init__(self, table: str, key: str) -> None:
        self.table = table
        self.key = key


def _returning_sql(columns: Sequence[str], dialect: Dialect) -> str:
    """Render a ``RETURNING`` clause, or raise when unsupported."""
    if not columns:
        return ""
    if not dialect.supports_returning:
        raise DialectError(f"{dialect.name}: RETURNING is not supported")
    return f" RETURNING {', '.join(columns)}"


def compile_statement(statement: Any, dialect: Dialect = POSTGRES) -> tuple[str, list[Any]]:
    """Render a statement node to ``(sql, params)``.

    Args:
        statement: :class:`Select`, :class:`Insert`, :class:`Update` or
            :class:`Delete`.
        dialect: Target dialect; PostgreSQL by default.

    Returns:
        The SQL text and its bound values, in placeholder order.

    Raises:
        DialectError: If the statement needs a feature the dialect lacks.
        TypeError: If the node is not a statement.
    """
    params: list[Any] = []
    return _compile_statement(statement, dialect, params), params


def _render_source(source: str | Subquery, dialect: Dialect, params: list[Any]) -> str:
    """Render a ``FROM`` source: a quoted table name or a derived table.

    Args:
        source: Table name, or a :class:`Subquery` wrapping the inner statement.
        dialect: Dialect of the enclosing statement.
        params: Parameter list of the enclosing statement — a derived table
            appends its own values here.

    Returns:
        ``users`` or ``(SELECT …) t0``.
    """
    if isinstance(source, Subquery):
        return f"({_compile_statement(source.statement, dialect, params)}) {source.alias}"
    return dialect.quote(source)


def _render_join(join: Join, dialect: Dialect, params: list[Any]) -> str:
    """Render one join clause: `` INNER JOIN posts t1 ON t1.user_id = t0.id``.

    Args:
        join: The join clause.
        dialect: Dialect of the enclosing statement.
        params: Parameter list of the enclosing statement (the ``ON`` condition
            may carry bound values).

    Returns:
        The clause, starting with a space.

    Raises:
        DialectError: If the dialect has no such join (``FULL`` on MySQL).
    """
    if join.kind == FULL and not dialect.supports_full_join:
        raise DialectError(f"{dialect.name}: FULL JOIN is not supported")
    alias = f" {join.alias}" if join.alias else ""
    condition = _render(join.on, dialect, params)
    source = _render_source(join.table, dialect, params)
    return f" {join.kind} JOIN {source}{alias} ON {condition}"


def _compile_statement(statement: Any, dialect: Dialect, params: list[Any]) -> str:
    """Render a statement into ``params`` and return the SQL text.

    The parameter list is passed in rather than created here so that a derived
    table (:class:`Subquery`) appends its values to the list of the statement
    that contains it — that keeps the placeholders of the whole query
    continuous (``$1`` inside the subquery, ``$2`` in the outer ``WHERE``).

    Args:
        statement: A statement node.
        dialect: Target dialect.
        params: List the bound values are appended to.

    Returns:
        The SQL text.
    """
    if isinstance(statement, Select):
        select_list = ", ".join(
            _render(item, dialect, params) if isinstance(item, Expr) else item
            for item in statement.columns
        )
        head = "SELECT DISTINCT" if statement.distinct else "SELECT"
        sql_text = (
            f"{head} {select_list} FROM {_render_source(statement.table, dialect, params)}"
        )
        for join in statement.joins:
            sql_text += _render_join(join, dialect, params)
        if statement.where:
            clause = " AND ".join(_render(item, dialect, params) for item in statement.where)
            sql_text += f" WHERE {clause}"
        if statement.group_by:
            terms = ", ".join(
                _render(item, dialect, params) if isinstance(item, Expr) else item
                for item in statement.group_by
            )
            sql_text += f" GROUP BY {terms}"
        if statement.having:
            clause = " AND ".join(_render(item, dialect, params) for item in statement.having)
            sql_text += f" HAVING {clause}"
        if statement.order:
            sql_text += " ORDER BY " + ", ".join(statement.order)
        if statement.limit is not None:
            sql_text += f" LIMIT {_bind(params, dialect, statement.limit)}"
        if statement.offset is not None:
            sql_text += f" OFFSET {_bind(params, dialect, statement.offset)}"
        return sql_text

    if isinstance(statement, Insert):
        if statement.rows < 1:
            raise ValueError("Insert.rows must be >= 1")
        table = dialect.quote(statement.table)
        columns = ", ".join(statement.columns)
        width = len(statement.columns)
        groups: list[str] = []
        for row in range(statement.rows):
            marks = ", ".join(
                dialect.placeholder(row * width + i) for i in range(1, width + 1)
            )
            groups.append(f"({marks})")
        sql_text = f"INSERT INTO {table} ({columns}) VALUES {', '.join(groups)}"
        if statement.conflict_key is not None:
            sql_text += dialect.conflict_clause(
                statement.conflict_key, statement.update_columns
            )
        return sql_text + _returning_sql(statement.returning, dialect)

    if isinstance(statement, Update):
        table = dialect.quote(statement.table)
        assignments = ", ".join(
            f"{dialect.quote(c)} = {dialect.placeholder(i)}"
            for i, c in enumerate(statement.columns, 1)
        )
        key = dialect.quote(statement.key)
        placeholder = dialect.placeholder(len(statement.columns) + 1)
        sql_text = f"UPDATE {table} SET {assignments} WHERE {key} = {placeholder}"
        return sql_text + _returning_sql(statement.returning, dialect)

    if isinstance(statement, Delete):
        table = dialect.quote(statement.table)
        key = dialect.quote(statement.key)
        return f"DELETE FROM {table} WHERE {key} = {dialect.placeholder(1)}"

    raise TypeError(f"not a SQL statement: {statement!r}")
