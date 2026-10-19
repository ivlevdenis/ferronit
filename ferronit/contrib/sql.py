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
    "BOOLEAN",
    "BYTES",
    "CAST_TYPES",
    "DATE",
    "EXCEPT",
    "FULL",
    "INNER",
    "INT",
    "INTERSECT",
    "JOIN_KINDS",
    "JSON",
    "LEFT",
    "MYSQL",
    "NUMERIC",
    "POSTGRES",
    "REAL",
    "RIGHT",
    "SET_KINDS",
    "SQLITE",
    "TEXT",
    "TIME",
    "TIMESTAMP",
    "UNION",
    "UUID",
    "AddColumn",
    "And",
    "As",
    "Between",
    "Cast",
    "Column",
    "Compare",
    "CreateIndex",
    "CreateTable",
    "Cte",
    "Delete",
    "Dialect",
    "DialectError",
    "DropTable",
    "Exists",
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
    "Over",
    "Param",
    "Paren",
    "PostgresDialect",
    "Raw",
    "Ref",
    "SQLiteDialect",
    "Select",
    "SetOp",
    "Subquery",
    "Update",
    "With",
    "compile_expr",
    "compile_statement",
    "renumber_placeholders",
]

_PLACEHOLDER_RE = re.compile(r"\$(\d+)")


class DialectError(Exception):
    """Raised when a statement needs a feature the dialect cannot express."""


# Канонические цели приведения типов: имя не зависит от движка (в дереве пишется
# `Cast(x, NUMERIC)`), а SQL-тип подбирает диалект — Dialect.cast_type. Движковые
# типы (JSON, UUID, TIME, BYTES) есть не везде: где движок их не знает — DialectError.
INT = "int"
TEXT = "text"
NUMERIC = "numeric"
REAL = "real"
BOOLEAN = "boolean"
DATE = "date"
TIMESTAMP = "timestamp"
TIME = "time"
BYTES = "bytes"
JSON = "json"
UUID = "uuid"

CAST_TYPES = (INT, TEXT, NUMERIC, REAL, BOOLEAN, DATE, TIMESTAMP, TIME, BYTES, JSON, UUID)

# Типы, у которых есть параметры точности (`numeric(10, 2)`); остальное — ValueError.
CAST_PARAMETERISED = (NUMERIC,)


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
        supports_set_op_all: Engine understands ``EXCEPT ALL``/``INTERSECT ALL``
            (``UNION ALL`` is understood everywhere; SQLite has no ``ALL``
            variants beyond ``UNION``).
        supports_set_op_parens: Engine accepts a parenthesised operand of a set
            operation (``(SELECT … LIMIT 1) UNION …``) — PostgreSQL and MySQL do,
            SQLite does not, so a branch with its own ``ORDER BY``/``LIMIT`` is
            an error there rather than "almost right" SQL.
        max_parameters: Placeholders one statement may carry — the cap a bulk
            ``INSERT`` has to respect (the caller turns it into a chunk size).
        supports_index_if_not_exists: Engine understands ``CREATE INDEX IF NOT
            EXISTS`` (MySQL does not) — a migration asking for it there is a
            :class:`DialectError`, not a statement that fails halfway.
        column_types: Canonical type (:data:`CAST_TYPES`) → column-type spelling.
            Separate from :attr:`cast_types` because the two roles differ: MySQL
            *casts* to ``SIGNED`` but *declares* ``int``, and a column may be
            ``uuid``/``json``/``text`` where a cast of that name is meaningless.
        excluded_keyword: Keyword for the conflicting row in an upsert.
    """

    name = "generic"
    supports_any = False
    supports_ilike = False
    supports_returning = False
    supports_upsert = False
    supports_placeholder_reuse = False
    supports_full_join = False
    supports_set_op_all = False
    supports_set_op_parens = False
    max_parameters = 32_767
    supports_index_if_not_exists = False
    cast_types: dict[str, str] = {}
    column_types: dict[str, str] = {}

    def column_type(
        self, name: str, precision: int | None = None, scale: int | None = None
    ) -> str:
        """Spell a canonical type (:data:`CAST_TYPES`) as a column type.

        Args:
            name: Canonical type name (``INT``, ``TEXT``, ``NUMERIC``, …).
            precision: Total digits, for parameterised types (``NUMERIC``).
            scale: Digits after the point, together with ``precision``.

        Returns:
            The engine's column-type spelling, with parameters when given.

        Raises:
            DialectError: If the engine has no such column type.
        """
        try:
            spelling = self.column_types[name]
        except KeyError:
            raise DialectError(f"{self.name}: no column type for {name!r}") from None
        if precision is None:
            return spelling
        if scale is None:
            return f"{spelling}({precision})"
        return f"{spelling}({precision}, {scale})"

    def primary_key_clause(self, column: Column) -> str:
        """Render the primary-key tail of a column definition.

        The base dialect knows only ``PRIMARY KEY``; engines that fill the key
        themselves override this with their identity form.

        Args:
            column: The column being defined.

        Returns:
            The clause, e.g. ``PRIMARY KEY``.
        """
        return "PRIMARY KEY"

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

    def cast_type(
        self,
        name: str,
        precision: int | None = None,
        scale: int | None = None,
    ) -> str:
        """Spell a canonical cast target (:data:`CAST_TYPES`) for this engine.

        The type map lives on the dialect because the same intent is written
        differently everywhere: ``cast(x, NUMERIC)`` is ``numeric`` on
        PostgreSQL, ``DECIMAL`` on MySQL and ``NUMERIC`` on SQLite; the money
        form is ``numeric(10, 2)`` / ``DECIMAL(10, 2)`` / ``NUMERIC(10, 2)``.

        Args:
            name: Canonical type name (``INT``, ``TEXT``, ``NUMERIC``, …).
            precision: Total digits, for parameterised types (``NUMERIC``).
            scale: Digits after the point, together with ``precision``.

        Returns:
            The engine's spelling of that type, with parameters when given.

        Raises:
            DialectError: If the engine has no faithful equivalent — SQLite has
                no date/time/uuid/json type and silently ignores such a cast
                (``CAST('…' AS UUID)`` gives ``0``), so it is refused loudly
                instead of producing an "almost right" value.
        """
        try:
            spelling = self.cast_types[name]
        except KeyError:
            raise DialectError(f"{self.name}: cast to {name!r} is not supported") from None
        if precision is None:
            return spelling
        if scale is None:
            return f"{spelling}({precision})"
        return f"{spelling}({precision}, {scale})"

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
    supports_set_op_all = True
    supports_set_op_parens = True
    supports_index_if_not_exists = True

    def primary_key_clause(self, column: Column) -> str:
        """Render an integer key as an identity column.

        Args:
            column: The column being defined.

        Returns:
            ``PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY`` for an integer key;
            plain ``PRIMARY KEY`` otherwise — a uuid/text key is filled by the
            application, not by the server.
        """
        if column.type_name != INT:
            return "PRIMARY KEY"
        return "PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY"
    cast_types = {
        INT: "integer",
        TEXT: "text",
        NUMERIC: "numeric",
        REAL: "double precision",
        BOOLEAN: "boolean",
        DATE: "date",
        TIMESTAMP: "timestamptz",
        TIME: "time",
        BYTES: "bytea",
        JSON: "jsonb",
        UUID: "uuid",
    }
    # У PostgreSQL роль совпадает: чем приводим — тем и объявляем.
    column_types = cast_types

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
    supports_set_op_all = True
    supports_set_op_parens = True
    max_parameters = 65_535
    cast_types = {
        # MySQL не умеет CAST(x AS INTEGER)/AS BOOLEAN: целые — SIGNED, признак — тоже SIGNED.
        # Типа uuid в MySQL нет (его роль играют CHAR(36)/BINARY(16)) — поэтому его здесь нет.
        INT: "SIGNED",
        TEXT: "CHAR",
        NUMERIC: "DECIMAL",
        REAL: "DOUBLE",
        BOOLEAN: "SIGNED",
        DATE: "DATE",
        TIMESTAMP: "DATETIME",
        TIME: "TIME",
        BYTES: "BINARY",
        JSON: "JSON",
    }
    column_types = {
        # Здесь роль другая, чем у cast_types: объявляем колонки, а не приводим
        # значения (MySQL приводит к SIGNED, но колонку объявляет int).
        # TEXT → varchar(255), а не text: у MySQL TEXT нельзя ни DEFAULT, ни
        # в индекс без префикса длины. Длиннее 255 — своим Raw("text").
        INT: "int",
        TEXT: "varchar(255)",
        NUMERIC: "decimal",
        REAL: "double",
        BOOLEAN: "tinyint(1)",
        DATE: "date",
        TIMESTAMP: "datetime",
        TIME: "time",
        BYTES: "blob",
        JSON: "json",
        UUID: "char(36)",  # отдельного типа uuid в MySQL нет — его роль играет char(36)
    }

    def primary_key_clause(self, column: Column) -> str:
        """Render an integer key as ``AUTO_INCREMENT`` (MySQL has no identity clause).

        Args:
            column: The column being defined.

        Returns:
            ``PRIMARY KEY AUTO_INCREMENT`` for an integer key, else ``PRIMARY KEY``.
        """
        if column.type_name != INT:
            return "PRIMARY KEY"
        return "PRIMARY KEY AUTO_INCREMENT"

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
    supports_index_if_not_exists = True
    # SQLITE_MAX_VARIABLE_NUMBER зависит от сборки: пол 32 766, в этой — 250 000
    # (замерено 16.10.2026). Берём пол, чтобы чанки были безопасны на любой сборке.
    max_parameters = 32_766
    cast_types = {
        # У SQLite типы — это «affinity», и незнакомое имя он молча принимает
        # (CAST('…' AS UUID) даёт 0, AS TIME — 12), поэтому в карте только те типы,
        # у которых поведение честное: текст, числа, BLOB.
        INT: "INTEGER",
        TEXT: "TEXT",
        NUMERIC: "NUMERIC",
        REAL: "REAL",
        BOOLEAN: "INTEGER",
        BYTES: "BLOB",
    }
    column_types = {
        # У SQLite нет дат/uuid/json как типов: их роль играют TEXT/BLOB по
        # соглашению (ISO-8601 для времени) — это осознанный выбор, в отличие от
        # CAST, где такое имя молча ничего не делает и потому отвергается.
        INT: "INTEGER",
        TEXT: "TEXT",
        NUMERIC: "NUMERIC",
        REAL: "REAL",
        BOOLEAN: "INTEGER",
        DATE: "TEXT",
        TIMESTAMP: "TEXT",
        TIME: "TEXT",
        BYTES: "BLOB",
        JSON: "TEXT",
        UUID: "TEXT",
    }

    def primary_key_clause(self, column: Column) -> str:
        """Render an integer key as ``INTEGER PRIMARY KEY AUTOINCREMENT`` (rowid alias).

        Args:
            column: The column being defined.

        Returns:
            ``PRIMARY KEY AUTOINCREMENT`` for an integer key, else ``PRIMARY KEY``.
        """
        if column.type_name != INT:
            return "PRIMARY KEY"
        return "PRIMARY KEY AUTOINCREMENT"

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

    def over(self, *, partition_by: Sequence[Any] = (), order: Sequence[str] = ()) -> Over:
        """Wrap in a window: ``Func("sum", "total").over(partition_by=["user_id"])``.

        Args:
            partition_by: ``PARTITION BY`` terms — column names or nodes.
            order: ``ORDER BY`` terms of the window, already rendered.

        Returns:
            The :class:`Over` node.
        """
        return Over(self, partition_by=partition_by, order=order)

    def as_(self, alias: str) -> As:
        """Name the expression as an output column: ``Func("sum", "x").as_("total")``.

        Args:
            alias: Output column name (quoted per dialect).

        Returns:
            The :class:`As` node.
        """
        return As(self, alias)

    def cast(
        self,
        type_name: str,
        *,
        precision: int | None = None,
        scale: int | None = None,
    ) -> Cast:
        """Convert to a canonical type: ``Func("sum", "total").cast(NUMERIC)``.

        Args:
            type_name: One of :data:`CAST_TYPES` (``INT``, ``TEXT``, ``NUMERIC``,
                ``REAL``, ``BOOLEAN``, ``DATE``, ``TIMESTAMP``, ``TIME``,
                ``BYTES``, ``JSON``, ``UUID``).
            precision: Total digits, for ``NUMERIC`` — e.g. ``precision=10,
                scale=2`` for money.
            scale: Digits after the point.

        Returns:
            The :class:`Cast` node.
        """
        return Cast(self, type_name, precision=precision, scale=scale)


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
            that support ``ANY``, or a :class:`Subquery` for ``IN (SELECT …)``
            (a subquery is never a bound parameter, on any engine).
        negated: Wrap the predicate in ``NOT (…)``.
    """

    __slots__ = ("column", "negated", "values")

    def __init__(
        self, column: str, values: Sequence[Any] | Subquery, *, negated: bool = False
    ) -> None:
        self.column = column
        if isinstance(values, Subquery):
            self.values: Sequence[Any] | Subquery = values
        else:
            self.values = list(values)
        self.negated = negated


class Exists(Expr):
    """``EXISTS (SELECT …)`` — the correlated-existence predicate.

    Args:
        subquery: The inner statement as a :class:`Subquery` (an alias is not
            printed in a predicate, so it may be left out).
    """

    __slots__ = ("subquery",)

    def __init__(self, subquery: Subquery) -> None:
        self.subquery = subquery


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


class Cast(Expr):
    """``CAST(<expression> AS <type>)`` with a portable type name.

    The target is a canonical name (:data:`CAST_TYPES`), not engine syntax: the
    dialect spells it (``NUMERIC`` → ``numeric`` on PostgreSQL, ``DECIMAL`` on
    MySQL) and refuses a target it cannot express faithfully — SQLite has no
    uuid/json/time type and silently returns nonsense for such a cast
    (``CAST('x' AS UUID)`` → ``0``), so it is a :class:`DialectError` there.

    Money is the reason ``precision``/``scale`` exist: ``Cast(x, NUMERIC,
    precision=10, scale=2)`` renders ``numeric(10, 2)`` on PostgreSQL,
    ``DECIMAL(10, 2)`` on MySQL and ``NUMERIC(10, 2)`` on SQLite, so the result
    comes back exact (``Decimal``) instead of a float.

    Args:
        expression: Expression to convert.
        type_name: One of :data:`CAST_TYPES`; validated on construction.
        precision: Total digits — only for :data:`CAST_PARAMETERISED` types.
        scale: Digits after the point; needs ``precision``.
    """

    __slots__ = ("expression", "precision", "scale", "type_name")

    def __init__(
        self,
        expression: Any,
        type_name: str,
        *,
        precision: int | None = None,
        scale: int | None = None,
    ) -> None:
        if type_name not in CAST_TYPES:
            raise ValueError(
                f"unknown cast type: {type_name!r}, expected one of {CAST_TYPES}"
            )
        if scale is not None and precision is None:
            raise ValueError("scale without precision: Cast(x, NUMERIC, precision=10, scale=2)")
        if precision is not None and type_name not in CAST_PARAMETERISED:
            raise ValueError(f"{type_name!r} takes no precision/scale")
        self.expression = expression
        self.type_name = type_name
        self.precision = precision
        self.scale = scale


class As(Expr):
    """``<expression> AS <alias>`` — names an output column.

    Needed when the outer query has to refer to a computed column (a window
    function, an aggregate, an arithmetic expression): without the name, a
    filter over it would have to repeat the whole expression.

    Args:
        expression: The expression to name (any node).
        alias: Output column name; quoted with the dialect, so an outer
            :class:`Ref` on the same name renders identically.
    """

    __slots__ = ("alias", "expression")

    def __init__(self, expression: Any, alias: str) -> None:
        if not alias:
            raise ValueError("As needs an alias")
        self.expression = expression
        self.alias = alias


class Over(Expr):
    """A windowed call: ``<function> OVER (PARTITION BY … ORDER BY …)``.

    Args:
        function: The windowed expression — usually :class:`Func`
            (``Func("row_number")``, ``Func("sum", "total")``).
        partition_by: ``PARTITION BY`` terms — column names (quoted per dialect)
            or nodes, as in :attr:`Select.group_by`.
        order: ``ORDER BY`` terms of the window, already rendered
            (``placed_at DESC``), as in :attr:`Select.order`.

    Note:
        No frame clause is modelled: without one the engine's default applies
        (with ``ORDER BY`` that is ``RANGE UNBOUNDED PRECEDING … CURRENT ROW``,
        i.e. a running total). A frame such as ``ROWS 3 PRECEDING`` can be
        written as a :class:`Raw` fragment until it is modelled.
    """

    __slots__ = ("function", "order", "partition_by")

    def __init__(
        self,
        function: Any,
        *,
        partition_by: Sequence[Any] = (),
        order: Sequence[str] = (),
    ) -> None:
        self.function = function
        self.partition_by = list(partition_by)
        self.order = list(order)


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
    if isinstance(node, Subquery):
        # Подзапрос в позиции предиката: `(SELECT …)`; алиас печатает только
        # FROM-источник (_render_source).
        return f"({_compile_statement(node.statement, dialect, params)})"
    if isinstance(node, SetOp):
        # Множественная операция в выражении — в скобках (IN (SELECT … UNION …)).
        return f"({_render_set_op(node, dialect, params)})"
    if isinstance(node, With):
        # CTE в выражении — тоже в скобках: IN (WITH … SELECT …).
        return f"({_render_with(node, dialect, params)})"
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
        if isinstance(node.values, Subquery):
            # `IN (SELECT …)`: подзапрос — не значение, ANY не применим нигде.
            inner = f"{column} IN {_render(node.values, dialect, params, offset)}"
        elif dialect.supports_any:
            inner = f"{column} = ANY({_bind(params, dialect, node.values, offset)})"
        elif not node.values:
            inner = f"{column} IN (NULL)"
        else:
            marks = ", ".join(_bind(params, dialect, value, offset) for value in node.values)
            inner = f"{column} IN ({marks})"
        return f"NOT ({inner})" if node.negated else inner
    if isinstance(node, Exists):
        return f"EXISTS {_render(node.subquery, dialect, params, offset)}"
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
    if isinstance(node, Cast):
        expression = _render(node.expression, dialect, params, offset)
        target = dialect.cast_type(node.type_name, node.precision, node.scale)
        return f"CAST({expression} AS {target})"
    if isinstance(node, As):
        expression = _render(node.expression, dialect, params, offset)
        return f"{expression} AS {dialect.quote(node.alias)}"
    if isinstance(node, Over):
        parts = []
        if node.partition_by:
            terms = ", ".join(
                _render(item, dialect, params, offset)
                if isinstance(item, Expr)
                else dialect.quote(item)
                for item in node.partition_by
            )
            parts.append(f"PARTITION BY {terms}")
        if node.order:
            parts.append("ORDER BY " + ", ".join(node.order))
        function = _render(node.function, dialect, params, offset)
        return f"{function} OVER ({' '.join(parts)})"
    raise TypeError(f"unsupported node: {node!r}")


# ── Statements ────────────────────────────────────────────────────────────────


LEFT = "LEFT"
INNER = "INNER"
RIGHT = "RIGHT"
FULL = "FULL"

JOIN_KINDS = (LEFT, INNER, RIGHT, FULL)


class Subquery(Expr):
    """A ``SELECT`` used inside another statement — a ``FROM`` source or a predicate.

    As a ``FROM`` source it is a derived table, rendered ``(…) alias``: the inner
    statement keeps its own ``WHERE``/``ORDER BY``/``LIMIT``, while the outer one
    refers to its columns through ``alias`` (:class:`Ref`). In a predicate —
    ``IN``, ``EXISTS``, comparison with a scalar — it renders as a bare
    ``(SELECT …)`` and the alias is not printed at all, so it may be left out.

    Args:
        statement: The inner statement (a :class:`Select`).
        alias: Alias of the derived table; required as a ``FROM`` source,
            unused in predicate position.
    """

    __slots__ = ("alias", "statement")

    def __init__(self, statement: Any, alias: str | None = None) -> None:
        self.statement = statement
        self.alias = alias


UNION = "UNION"
EXCEPT = "EXCEPT"
INTERSECT = "INTERSECT"

SET_KINDS = (UNION, EXCEPT, INTERSECT)


class SetOp(Expr):
    """Two statements combined by ``UNION``/``EXCEPT``/``INTERSECT``.

    The result is a row set, so it can stand for a statement (top level, or
    inside :class:`Subquery`) and in an expression (``IN (SELECT … UNION …)``).

    Args:
        left: First operand — a :class:`Select` or a nested :class:`SetOp`.
        right: Second operand.
        kind: One of :data:`SET_KINDS`; ``UNION`` by default.
        all: Emit the ``ALL`` variant (duplicates kept). ``UNION ALL`` works on
            every dialect; ``EXCEPT ALL``/``INTERSECT ALL`` only where
            :attr:`Dialect.supports_set_op_all` is set — otherwise
            :class:`DialectError`.
        order: ``ORDER BY`` terms of the whole compound (not of one operand).
        limit: Row limit of the compound; bound as a parameter when set.
        offset: Row offset of the compound; bound as a parameter when set.

    Note:
        An operand carrying its own ``ORDER BY``/``LIMIT``, or a nested set
        operation, is parenthesised — without the parentheses those clauses
        attach to the whole compound. SQLite accepts no such parentheses
        (:attr:`Dialect.supports_set_op_parens`), so there it is
        :class:`DialectError`.
    """

    __slots__ = ("all", "kind", "left", "limit", "offset", "order", "right")

    def __init__(
        self,
        left: Any,
        right: Any,
        *,
        kind: str = UNION,
        all: bool = False,
        order: Sequence[str] = (),
        limit: int | None = None,
        offset: int | None = None,
    ) -> None:
        if kind not in SET_KINDS:
            raise ValueError(f"unknown set operation: {kind!r}")
        self.left = left
        self.right = right
        self.kind = kind
        self.all = all
        self.order = list(order)
        self.limit = limit
        self.offset = offset


class Cte:
    """One named sub-statement of a ``WITH`` clause.

    Args:
        name: CTE name; afterwards it is referenced as a table name
            (``Select(name, …)``) — no separate node is needed for that.
        statement: The sub-statement (a :class:`Select` or :class:`SetOp`).
    """

    __slots__ = ("name", "statement")

    def __init__(self, name: str, statement: Any) -> None:
        if not name:
            raise ValueError("a CTE needs a name")
        self.name = name
        self.statement = statement


class With(Expr):
    """``WITH name AS (SELECT …) …`` — common table expressions.

    The CTEs are rendered before the main statement, in the order given, so
    parameter numbering stays continuous across the whole query.

    Args:
        statement: The main statement, which sees the CTEs by name.
        ctes: The CTE list (:class:`Cte`), in order.
        recursive: Emit ``WITH RECURSIVE`` (a CTE referring to itself) —
            PostgreSQL, MySQL 8+ and SQLite all understand it.

    Note:
        Usable anywhere a statement is (top level, inside :class:`Subquery`,
        around a :class:`SetOp`), and in an expression as ``(WITH … SELECT …)``.
    """

    __slots__ = ("ctes", "recursive", "statement")

    def __init__(
        self, statement: Any, *, ctes: Sequence[Cte], recursive: bool = False
    ) -> None:
        if not ctes:
            raise ValueError("WITH needs at least one CTE")
        names = [cte.name for cte in ctes]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate CTE name: {names}")
        self.statement = statement
        self.ctes = list(ctes)
        self.recursive = recursive


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
        alias: Alias of a plain-table ``FROM`` source (``FROM users t0``); a
            derived table carries its own alias inside :class:`Subquery`.
    """

    __slots__ = (
        "alias",
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
        alias: str | None = None,
    ) -> None:
        if alias is not None and isinstance(table, Subquery):
            # Как и у Join: алиас задаётся ровно один раз (иначе «t0 t0»).
            raise ValueError("derived table: alias belongs to Subquery")
        self.table = table
        self.alias = alias
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
    """``DELETE`` by one key column, optionally ``RETURNING``.

    ``RETURNING`` is what makes a delete tell you *what* it removed (or that it
    removed nothing) in one round-trip; engines without it raise
    :class:`DialectError` rather than silently dropping the clause.

    Args:
        table: Table name.
        key: Key column of the ``WHERE`` clause.
        returning: Columns of the ``RETURNING`` clause (empty = none); ``("*",)``
            asks for the whole row.
    """

    __slots__ = ("key", "returning", "table")

    def __init__(self, table: str, key: str, *, returning: Sequence[str] = ()) -> None:
        self.table = table
        self.key = key
        self.returning = list(returning)


class Column:
    """One column of a ``CREATE TABLE`` / ``ADD COLUMN``.

    Args:
        name: Column name (quoted per dialect).
        type_name: Canonical type (:data:`CAST_TYPES`) — the dialect spells the
            column type (``INT`` → ``integer`` / ``int`` / ``INTEGER``).
        pk: Primary key. An integer key is filled by the engine itself (identity
            / ``AUTO_INCREMENT`` / ``rowid``); any other type gets a plain
            ``PRIMARY KEY`` and the value comes from the application.
        nullable: ``False`` adds ``NOT NULL``.
        default: Literal for ``DEFAULT``, or a parameter-free :class:`Raw`
            fragment (``Raw("now()")``) when a literal will not do — DDL has no
            bound values, so a fragment carrying values is refused.
        unique: Add ``UNIQUE`` to the column.
        precision: Total digits, for parameterised types (``NUMERIC``).
        scale: Digits after the point.
    """

    __slots__ = ("default", "name", "nullable", "pk", "precision", "scale", "type_name", "unique")

    def __init__(
        self,
        name: str,
        type_name: str,
        *,
        pk: bool = False,
        nullable: bool = True,
        default: Any = None,
        unique: bool = False,
        precision: int | None = None,
        scale: int | None = None,
    ) -> None:
        if not name:
            raise ValueError("a column needs a name")
        if type_name not in CAST_TYPES:
            raise ValueError(
                f"unknown column type: {type_name!r}, expected one of {CAST_TYPES}"
            )
        if isinstance(default, Raw) and default.params:
            raise ValueError(
                "DEFAULT takes no bound values: pass a literal or Raw(sql) without params"
            )
        self.name = name
        self.type_name = type_name
        self.pk = pk
        self.nullable = nullable
        self.default = default
        self.unique = unique
        self.precision = precision
        self.scale = scale


class CreateTable:
    """``CREATE TABLE`` — one :class:`Column` per column.

    Args:
        table: Table name.
        columns: Column definitions, in order.
        if_not_exists: Emit ``IF NOT EXISTS``: a migration that has already run
            once must not blow up the second time.
    """

    __slots__ = ("columns", "if_not_exists", "table")

    def __init__(
        self, table: str, columns: Sequence[Column], *, if_not_exists: bool = True
    ) -> None:
        if not columns:
            raise ValueError("CREATE TABLE needs at least one column")
        names = [column.name for column in columns]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate column name: {names}")
        self.table = table
        self.columns = list(columns)
        self.if_not_exists = if_not_exists


class AddColumn:
    """``ALTER TABLE … ADD COLUMN …``.

    Args:
        table: Table name.
        column: The column to add.
    """

    __slots__ = ("column", "table")

    def __init__(self, table: str, column: Column) -> None:
        self.table = table
        self.column = column


class CreateIndex:
    """``CREATE [UNIQUE] INDEX`` over one table's columns.

    Args:
        name: Index name.
        table: Table name.
        columns: Indexed columns, in order.
        unique: Emit ``UNIQUE``.
        if_not_exists: Emit ``IF NOT EXISTS`` where the engine has that form.
            Off by default: MySQL has no such form, so the portable default is
            plain ``CREATE INDEX`` (re-running a migration is prevented by the
            bookkeeping table, not by this clause); turning it on for MySQL is a
            :class:`DialectError` rather than a statement that fails halfway.
    """

    __slots__ = ("columns", "if_not_exists", "name", "table", "unique")

    def __init__(
        self,
        name: str,
        table: str,
        columns: Sequence[str],
        *,
        unique: bool = False,
        if_not_exists: bool = False,
    ) -> None:
        if not columns:
            raise ValueError("CREATE INDEX needs at least one column")
        self.name = name
        self.table = table
        self.columns = list(columns)
        self.unique = unique
        self.if_not_exists = if_not_exists


class DropTable:
    """``DROP TABLE [IF EXISTS]``.

    Args:
        table: Table name.
        if_exists: Emit ``IF EXISTS`` (the rollback of a migration may run twice).
    """

    __slots__ = ("if_exists", "table")

    def __init__(self, table: str, *, if_exists: bool = True) -> None:
        self.table = table
        self.if_exists = if_exists


def _literal(value: Any) -> str:
    """Render a ``DEFAULT`` value — DDL carries no bound parameters.

    A :class:`Raw` fragment goes in as written (``Raw("now()")``); a string is
    quoted with the only escaping DDL itself needs.
    """
    if isinstance(value, Raw):
        return value.sql
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _render_column(column: Column, dialect: Dialect) -> str:
    """Render one column definition: name, type, key, nullability, default, unique."""
    parts = [
        dialect.quote(column.name),
        dialect.column_type(column.type_name, column.precision, column.scale),
    ]
    if column.pk:
        parts.append(dialect.primary_key_clause(column))
    if not column.nullable:
        parts.append("NOT NULL")
    if column.default is not None:
        parts.append(f"DEFAULT {_literal(column.default)}")
    if column.unique:
        parts.append("UNIQUE")
    return " ".join(parts)


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
        statement: :class:`Select`, :class:`Insert`, :class:`Update`,
            :class:`Delete`, a set operation (:class:`SetOp`), a CTE
            (:class:`With`) or DDL (:class:`CreateTable`, :class:`AddColumn`,
            :class:`CreateIndex`, :class:`DropTable`).
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

    Raises:
        ValueError: If the derived table has no alias — ``FROM (SELECT …)``
            without one is not valid SQL.
    """
    if isinstance(source, Subquery):
        if source.alias is None:
            raise ValueError("derived table needs an alias: Subquery(statement, alias)")
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


def _render_set_operand(statement: Any, dialect: Dialect, params: list[Any]) -> str:
    """Render one operand of a set operation, parenthesised when it has to be.

    An operand with its own ``ORDER BY``/``LIMIT``/``OFFSET``, or a nested set
    operation, must be parenthesised: without the parentheses those clauses
    attach to the whole compound. SQLite accepts no parentheses there, so it
    gets a loud error instead of an "almost right" statement (its limit —
    upgrade when a dialect appears that can express it another way).

    Args:
        statement: The operand statement.
        dialect: Dialect of the whole statement.
        params: Parameter list of the whole statement.

    Returns:
        The operand's SQL, in parentheses when needed.

    Raises:
        DialectError: If parentheses are needed and the dialect has none.
    """
    parenthesised = isinstance(statement, SetOp) or (
        isinstance(statement, Select)
        and (bool(statement.order) or statement.limit is not None or statement.offset is not None)
    )
    if not parenthesised:
        return _compile_statement(statement, dialect, params)
    if not dialect.supports_set_op_parens:
        raise DialectError(
            f"{dialect.name}: an operand of a set operation cannot be parenthesised "
            "(branch with its own ORDER BY/LIMIT, or a nested operation)"
        )
    return f"({_compile_statement(statement, dialect, params)})"


def _render_set_op(statement: SetOp, dialect: Dialect, params: list[Any]) -> str:
    """Render ``SELECT … UNION ALL SELECT …`` plus the compound's own tail.

    Args:
        statement: The set operation.
        dialect: Dialect of the whole statement.
        params: Parameter list the bound values are appended to.

    Returns:
        The SQL text, without outer parentheses.

    Raises:
        DialectError: If the dialect has no ``ALL`` variant for this operator,
            or cannot parenthesise an operand.
    """
    if statement.all and statement.kind != UNION and not dialect.supports_set_op_all:
        raise DialectError(f"{dialect.name}: {statement.kind} ALL is not supported")
    keyword = f"{statement.kind} ALL" if statement.all else statement.kind
    left = _render_set_operand(statement.left, dialect, params)
    right = _render_set_operand(statement.right, dialect, params)
    text = f"{left} {keyword} {right}"
    if statement.order:
        text += " ORDER BY " + ", ".join(statement.order)
    if statement.limit is not None:
        text += f" LIMIT {_bind(params, dialect, statement.limit)}"
    if statement.offset is not None:
        text += f" OFFSET {_bind(params, dialect, statement.offset)}"
    return text


def _render_with(statement: With, dialect: Dialect, params: list[Any]) -> str:
    """Render ``WITH name AS (…) <statement>``.

    The CTEs are rendered first, in order, so their values land in ``params``
    before those of the main statement — the placeholders stay continuous.

    Args:
        statement: The ``WITH`` node.
        dialect: Dialect of the whole statement.
        params: Parameter list the bound values are appended to.

    Returns:
        The SQL text, without outer parentheses.
    """
    ctes = ", ".join(
        f"{dialect.quote(cte.name)} AS ({_compile_statement(cte.statement, dialect, params)})"
        for cte in statement.ctes
    )
    keyword = "WITH RECURSIVE" if statement.recursive else "WITH"
    body = _compile_statement(statement.statement, dialect, params)
    return f"{keyword} {ctes} {body}"


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
    if isinstance(statement, SetOp):
        return _render_set_op(statement, dialect, params)

    if isinstance(statement, With):
        return _render_with(statement, dialect, params)

    if isinstance(statement, Select):
        select_list = ", ".join(
            _render(item, dialect, params) if isinstance(item, Expr) else item
            for item in statement.columns
        )
        head = "SELECT DISTINCT" if statement.distinct else "SELECT"
        source = _render_source(statement.table, dialect, params)
        if statement.alias:
            source += f" {statement.alias}"
        sql_text = f"{head} {select_list} FROM {source}"
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
        sql_text = f"DELETE FROM {table} WHERE {key} = {dialect.placeholder(1)}"
        return sql_text + _returning_sql(statement.returning, dialect)

    if isinstance(statement, CreateTable):
        head = "CREATE TABLE IF NOT EXISTS" if statement.if_not_exists else "CREATE TABLE"
        columns = ", ".join(_render_column(column, dialect) for column in statement.columns)
        return f"{head} {dialect.quote(statement.table)} ({columns})"

    if isinstance(statement, AddColumn):
        return (
            f"ALTER TABLE {dialect.quote(statement.table)}"
            f" ADD COLUMN {_render_column(statement.column, dialect)}"
        )

    if isinstance(statement, CreateIndex):
        if statement.if_not_exists and not dialect.supports_index_if_not_exists:
            raise DialectError(
                f"{dialect.name}: CREATE INDEX IF NOT EXISTS is not supported"
            )
        unique = "UNIQUE " if statement.unique else ""
        exists = "IF NOT EXISTS " if statement.if_not_exists else ""
        columns = ", ".join(dialect.quote(name) for name in statement.columns)
        return (
            f"CREATE {unique}INDEX {exists}{dialect.quote(statement.name)}"
            f" ON {dialect.quote(statement.table)} ({columns})"
        )

    if isinstance(statement, DropTable):
        head = "DROP TABLE IF EXISTS" if statement.if_exists else "DROP TABLE"
        return f"{head} {dialect.quote(statement.table)}"

    raise TypeError(f"not a SQL statement: {statement!r}")
