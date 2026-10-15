"""Declarative row models for the raw asyncpg layer.

Why this module exists: ``RawRepository`` (``rawdb.py``) returns ``dict`` rows,
and constructing that ``dict`` costs extra work compared with positional tuple
unpacking. A model declared once knows its columns and their order, so the
row-to-instance mapping is a plain positional ``cls(*row)`` — no ``dict``, no
reflection and no code generation on the hot path.

A model is just a subclass of ``Model`` with annotated fields in ``SELECT *``
column order; the metaclass keeps it an immutable ``msgspec.Struct`` (fast
construction and C-level JSON) and captures the column order once at
class-definition time:

    class User(Model):
        id: int = 0
        name: str = ""
        email: str = ""

The table name defaults to the pluralised, lowercased class name (``User`` →
``users``, ``Category`` → ``categories``, ``Person`` → ``people``). Override it
per call (``select(User, "app_users")``, ``RawModelRepository(conn, User,
table="app_users")``) or declaratively on the model itself:

    class User(Model):
        __table__ = "app_users"
        id: int = 0
"""

from __future__ import annotations

import builtins
import re
from collections.abc import Sequence
from functools import lru_cache
from typing import Annotated, Any, ClassVar, get_args, get_origin

import msgspec
from msgspec.structs import StructMeta  # type: ignore[attr-defined]
from msgspec.structs import fields as struct_fields

from ferronit.contrib import sql
from ferronit.contrib.rawdb import Condition, Pool, RawUnitOfWork
from ferronit.hexagonal import Adapter

__all__ = [
    "Column",
    "IdentityMap",
    "Mapper",
    "Model",
    "OnPair",
    "Query",
    "RawModelRepository",
    "Relation",
    "Repository",
    "select",
]


# ── Table name resolution ─────────────────────────────────────────────

_IRREGULAR = {
    "child": "children",
    "foot": "feet",
    "goose": "geese",
    "man": "men",
    "mouse": "mice",
    "ox": "oxen",
    "person": "people",
    "tooth": "teeth",
    "woman": "women",
}

_UNCOUNTABLE = {
    "data",
    "deer",
    "equipment",
    "fish",
    "information",
    "news",
    "series",
    "sheep",
    "species",
}


def _pluralize(name: str) -> str:
    """Pluralise an English table name with the common (not exhaustive) rules.

    Handles the regular ``-s``/``-es``/``-ies`` endings plus a short list of
    irregular and uncountable nouns. Anything not covered simply gets an ``s``.
    """
    lowered = name.lower()
    if lowered in _UNCOUNTABLE:
        return name
    if lowered in _IRREGULAR:
        return _IRREGULAR[lowered]
    if len(lowered) >= 2 and lowered.endswith("y") and lowered[-2] not in "aeiou":
        return name[:-1] + "ies"
    if lowered.endswith(("s", "x", "z", "ch", "sh")):
        return name + "es"
    return name + "s"


def _resolve_table(model: type[Model]) -> str:
    """Resolve a model's table: ``__table__`` when set, else pluralised name."""
    explicit = getattr(model, "__table__", None)
    if explicit:
        return explicit
    return _pluralize(model.__name__.lower())


class Relation:
    """Декларация связи модели чтения — поле грузится отдельным запросом.

    Объявляется через ``Annotated``: ``items: Annotated[list[OrderItem], Relation("order_id")]``.
    Связь всегда грузится при чтении модели (без lazy/eager — поле есть, значит нужно).

    Args:
        fk: Колонка в дочерней таблице, ссылающаяся на ``pk`` родителя.
        pk: Колонка в родительской таблице (по умолчанию ``"id"``).
    """

    __slots__ = ("fk", "pk")

    def __init__(self, fk: str, *, pk: str = "id") -> None:
        self.fk = fk
        self.pk = pk


def _unwrap_relation(t: Any) -> tuple[Any, Relation | None]:
    """Развернуть ``Annotated[T, Relation(...)]`` → ``(T, Relation)`` либо ``(t, None)``."""
    if get_origin(t) is Annotated:
        base = get_args(t)[0]
        for meta in get_args(t)[1:]:
            if isinstance(meta, Relation):
                return base, meta
        return base, None
    return t, None


def _relation_target(base: Any) -> tuple[type[Model], bool]:
    """Из ``list[Child]``/``Child`` → ``(Child, many)``."""
    if get_origin(base) is list:
        return get_args(base)[0], True
    return base, False


class ModelMeta(StructMeta):
    """Metaclass: каждый подкласс ``Model`` — ``msgspec.Struct`` + колонки по порядку."""

    def __new__(mcs, name, bases, ns, **kw: Any):
        cls = super().__new__(mcs, name, bases, ns, **kw)
        if name != "Model":
            columns: list[str] = []
            relations: dict[str, tuple[type[Model], str, str, bool]] = {}
            for f in struct_fields(cls):
                base, rel = _unwrap_relation(f.type)
                if rel is not None:
                    child, many = _relation_target(base)
                    relations[f.name] = (child, rel.fk, rel.pk, many)
                else:
                    columns.append(f.name)
            cls.__columns__ = tuple(columns)
            cls.__select_columns__ = ", ".join(columns)
            cls.__relations__ = relations
            cls.c = _Columns({name: Column(name, cls) for name in columns})
        return cls


class Model(msgspec.Struct, metaclass=ModelMeta):
    """Base class for a declarative row model.

    Subclass it and annotate the columns as fields, in the order ``SELECT *``
    returns them. The subclass is an immutable ``msgspec.Struct``: fast
    positional construction and C-level JSON serialisation.
    """

    __table__ = None
    __relations__: ClassVar[dict] = {}

    @classmethod
    def from_row(cls, row: Any) -> Any:
        """Build an instance from a positional row (e.g. an asyncpg ``Record``)."""
        return cls(*row)

    def to_tuple(self) -> tuple[Any, ...]:
        """Return the field values in declaration order."""
        return msgspec.structs.astuple(self)


class Column:
    """Typed column reference; comparison operators produce expression nodes.

    Columns are reached through ``Model.c`` (e.g. ``User.c.age``), so field names
    stay type-checked at authoring time instead of being raw SQL strings. Every
    operator returns a :mod:`ferronit.contrib.sql` node, which the query's
    dialect renders — so the same filter compiles for PostgreSQL or SQLite.
    Comparing two columns (``A.c.id == B.c.a_id``) yields an :class:`OnPair` for
    an explicit ``Query.join``.
    """

    __slots__ = ("_model", "name")

    def __init__(self, name: str, model: type[Model] | None = None) -> None:
        self.name = name
        self._model = model

    def __gt__(self, other: Any) -> sql.Compare:
        return sql.Compare(self.name, ">", other)

    def __ge__(self, other: Any) -> sql.Compare:
        return sql.Compare(self.name, ">=", other)

    def __lt__(self, other: Any) -> sql.Compare:
        return sql.Compare(self.name, "<", other)

    def __le__(self, other: Any) -> sql.Compare:
        return sql.Compare(self.name, "<=", other)

    def __eq__(self, other: Any) -> sql.Compare | sql.IsNull | OnPair:  # type: ignore[override]
        if other is None:
            return sql.IsNull(self.name)
        if isinstance(other, Column):
            return OnPair(self, other)
        return sql.Compare(self.name, "=", other)

    def __ne__(self, other: Any) -> sql.Compare | sql.IsNull:  # type: ignore[override]
        if other is None:
            return sql.IsNull(self.name, negated=True)
        return sql.Compare(self.name, "!=", other)

    def in_(self, values: Sequence[Any]) -> sql.InList:
        """``IN``: поле входит в набор значений (на PostgreSQL ``col = ANY($1)``)."""
        return sql.InList(self.name, list(values))

    def not_in(self, values: Sequence[Any]) -> sql.InList:
        """``NOT IN``: поле не входит в набор значений."""
        return sql.InList(self.name, list(values), negated=True)

    def like(self, pattern: str) -> sql.Like:
        """``LIKE`` с wildcard-паттерном (``%``/``_``)."""
        return sql.Like(self.name, pattern)

    def ilike(self, pattern: str) -> sql.Like:
        """``ILIKE`` — LIKE без учёта регистра (диалект без ILIKE даёт ``LIKE``)."""
        return sql.Like(self.name, pattern, insensitive=True)

    def is_null(self) -> sql.IsNull:
        """``IS NULL``."""
        return sql.IsNull(self.name)

    def is_not_null(self) -> sql.IsNull:
        """``IS NOT NULL``."""
        return sql.IsNull(self.name, negated=True)

    def between(self, low: Any, high: Any) -> sql.Between:
        """``col BETWEEN $1 AND $2``."""
        return sql.Between(self.name, low, high)

    def asc(self) -> str:
        """Ordering term: ascending."""
        return f"{self.name} ASC"

    def desc(self) -> str:
        """Ordering term: descending."""
        return f"{self.name} DESC"


class OnPair:
    """Comparison of two columns for an explicit ``Query.join`` (``A.c.x == B.c.y``).

    Holds the left and right columns together with their models; when a join is
    compiled, each side is qualified with its table alias (``t0``/``t1``/…).
    """

    __slots__ = ("left", "right")

    def __init__(self, left: Column, right: Column) -> None:
        self.left = left
        self.right = right


class _Columns:
    """Namespace of a model's columns, reached as ``Model.c``."""

    __slots__ = ("_cols",)
    _cols: dict[str, Column]

    def __init__(self, cols: dict[str, Column]) -> None:
        object.__setattr__(self, "_cols", cols)

    def __getattr__(self, name: str) -> Column:
        try:
            return self._cols[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class Query:
    """A ``SELECT`` over one model, compiled to SQL by an expression tree.

    Usage:
        q = select(User).where(User.c.age > 18).order_by(User.c.name).limit(50)
        rows = await repo.fetch(q)

    Filters are nodes from :mod:`ferronit.contrib.sql`, rendered for the
    query's dialect — PostgreSQL by default, another engine by passing
    ``dialect`` to :meth:`compile` (the tree itself is dialect-free).
    """

    __slots__ = (
        "_dialect",
        "_distinct",
        "_group_by",
        "_having",
        "_joins",
        "_limit",
        "_model",
        "_offset",
        "_order",
        "_select",
        "_table",
        "_where",
    )

    def __init__(
        self,
        model: type[Model],
        table: str | None = None,
        dialect: sql.Dialect | None = None,
    ) -> None:
        self._model = model
        self._table = table or _resolve_table(model)
        self._dialect = dialect or sql.POSTGRES
        # Предкомпиляция статики: колонки известны на уровне класса.
        self._select: list[Any] = list(model.__columns__)
        self._where: list[Any] = []
        self._distinct = False
        self._group_by: list[Any] = []
        self._having: list[Any] = []
        self._order: list[str] = []
        self._limit: int | None = None
        self._offset: int | None = None
        self._joins: list[tuple[type[Model], OnPair | None, str | None, str]] = []

    def where(self, *conditions: Any) -> Query:
        """Add ``AND``-combined filter conditions.

        Условие может быть узлом (сравнения через ``User.c.*``, ``|``/``&``/``~``),
        готовым :class:`~ferronit.contrib.rawdb.Condition` с сырым SQL или строкой
        мини-DSL — она парсится и параметры биндятся:

            select(User).where("age > 18 and (name ilike '%a%' or status in ('x','y'))")

        Имена колонок проверяются по модели; значения уходят как bound-параметры
        (не вставляются в SQL).
        """
        for condition in conditions:
            if isinstance(condition, str):
                condition = parse_where(condition, self._model.__columns__)
            self._where.append(condition)
        return self

    def order_by(self, *specs: Column | str) -> Query:
        """Set ordering by columns (ascending by default) or raw terms."""
        self._order.extend(s.name if isinstance(s, Column) else s for s in specs)
        return self

    def distinct(self) -> Query:
        """Select distinct rows (``SELECT DISTINCT``)."""
        self._distinct = True
        return self

    def group_by(self, *terms: Column | str | sql.Expr) -> Query:
        """Group rows for aggregates (``GROUP BY``).

        Args:
            *terms: ``Model.c`` columns, raw column names, or expression nodes.

        Returns:
            This query (mutated, for chaining).
        """
        self._group_by.extend(t.name if isinstance(t, Column) else t for t in terms)
        return self

    def having(self, *conditions: sql.Expr) -> Query:
        """Filter groups after aggregating (``HAVING``).

        Unlike :meth:`where` the conditions are tree nodes, not the string DSL: the
        DSL knows columns only, while a group filter usually compares an aggregate —
        ``having(sql.Compare(sql.Func("count"), ">", 2))``.
        """
        self._having.extend(conditions)
        return self

    def limit(self, n: int) -> Query:
        """Limit the number of rows."""
        self._limit = n
        return self

    def offset(self, n: int) -> Query:
        """Skip ``n`` rows before returning."""
        self._offset = n
        return self

    def join(
        self,
        other: type[Model],
        *,
        on: OnPair | None = None,
        fk: str | None = None,
        pk: str = "id",
    ) -> Query:
        """Add an explicit ``LEFT JOIN`` of ``other`` to the query.

        Use either ``fk`` (foreign key in ``other`` → ``pk`` of this model) for
        a plain FK join, or ``on`` — a column-to-column comparison
        (``Parent.c.id == Child.c.parent_id``). The joined rows are read with
        ``repo.fetch_joined(query)``, which returns ``(parent, [children])``
        tuples.

        Args:
            other: Model to join.
            on: Column-to-column condition (``Parent.c.x == Child.c.y``).
            fk: Foreign-key column name in ``other`` (shortcut for ``on``).
            pk: Primary-key column of this model (default ``"id"``).

        Returns:
            This query (mutated, for chaining).
        """
        self._joins.append((other, on, fk, pk))
        return self

    def first(self) -> Query:
        """Ограничить одной строкой (``LIMIT 1``)."""
        return self.limit(1)

    one = first  # алиас: `select(...).one()` читается естественнее

    def columns(self, *names: str | sql.Expr) -> Query:
        """Выбрать конкретные колонки вместо всех полей модели.

        Принимает и имена, и узлы (``sql.Func("count")``) — для проекций и
        агрегатов результат забирается через ``repo.fetch_rows()``.
        """
        self._select = list(names)
        return self

    def count(self) -> Query:
        """Агрегат ``count(*)``; результат забирается через ``repo.fetch_value()``."""
        self._select = ["count(*)"]
        return self

    def aggregate(self, expression: str) -> Query:
        """Произвольный агрегат (``sum(col)``, ``avg(col)``, ...); через ``fetch_value()``."""
        self._select = [expression]
        return self

    def compile(self, dialect: sql.Dialect | None = None) -> tuple[str, list[Any]]:
        """Compile to ``(sql, params)`` with continuous ``$N`` placeholders.

        Args:
            dialect: Render for this dialect; the query's own dialect
                (PostgreSQL unless the query was built with another one).

        Returns:
            The SQL text and its bound values in placeholder order.
        """
        statement = sql.Select(
            self._table,
            self._select,
            where=[sql.Paren(item) for item in self._where],
            distinct=self._distinct,
            group_by=self._group_by,
            having=[sql.Paren(item) for item in self._having],
            order=self._order,
            limit=self._limit,
            offset=self._offset,
        )
        sql_text, params = sql.compile_statement(statement, dialect or self._dialect)
        if self._joins:
            sql_text, params = _wrap_joins(sql_text, params, self._model, self._joins)
        return sql_text, params


def select(
    model: type[Model], table: str | None = None, *, dialect: sql.Dialect | None = None
) -> Query:
    """Start a ``SELECT`` over ``model``.

    Args:
        model: Model class whose columns define the select list.
        table: Table name override; the model's resolved name by default.
        dialect: Dialect for the compiled SQL (PostgreSQL by default).

    Returns:
        A fresh :class:`Query`.
    """
    return Query(model, table, dialect)


def _resolve_on_pair(
    on: OnPair, parent_model: type[Model], child_model: type[Model], alias: str
) -> str:
    """Развернуть ``OnPair`` в ``t{i}.child_col = t0.parent_col`` (или наоборот)."""
    left, right = on.left, on.right
    if left._model is parent_model and right._model is child_model:
        return f"{alias}.{right.name} = t0.{left.name}"
    if right._model is parent_model and left._model is child_model:
        return f"{alias}.{left.name} = t0.{right.name}"
    raise ValueError(
        "join on: колонки должны принадлежать родителю и джойн-модели соответственно"
    )


def _wrap_joins(
    parent_sql: str,
    params: list[Any],
    parent_model: type[Model],
    joins: list[tuple[type[Model], OnPair | None, str | None, str]],
) -> tuple[str, list[Any]]:
    """Обернуть SELECT по родителю в явные ``LEFT JOIN``-ы из ``Query.join``.

    Родительский SELECT (с WHERE/ORDER/LIMIT/OFFSET) уходит в подзапрос ``t0``,
    каждая джойн-модель получает алиас ``t1``/``t2``/… с условием ``ON``.
    """
    cols = [f"t0.{c}" for c in parent_model.__columns__]
    clauses: list[str] = []
    for index, (child, on, fk, pk) in enumerate(joins, start=1):
        alias = f"t{index}"
        cols.extend(f"{alias}.{c}" for c in child.__columns__)
        if fk is not None:
            on_sql = f"{alias}.{fk} = t0.{pk}"
        else:
            on_sql = _resolve_on_pair(on, parent_model, child, alias)  # type: ignore[arg-type]
        clauses.append(f" LEFT JOIN {_resolve_table(child)} {alias} ON {on_sql}")
    return f"SELECT {', '.join(cols)} FROM ({parent_sql}) t0{''.join(clauses)}", params


# --- WHERE mini-DSL ---------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<num>-?\d+(?:\.\d+)?)
  | (?P<str>'(?:[^']|'')*'|"(?:[^"]|"")*")
  | (?P<op><=|>=|!=|=|<|>)
  | (?P<punct>[(),])
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.VERBOSE,
)

_RESERVED = {
    "and", "or", "not", "in", "like", "ilike", "is", "null",
    "between", "true", "false",
}


def _tokenize(expression: str) -> list[tuple[str, Any]]:
    tokens: list[tuple[str, Any]] = []
    pos = 0
    while pos < len(expression):
        match = _TOKEN_RE.match(expression, pos)
        if match is None:
            raise ValueError(f"Unexpected character at {pos}: {expression[pos]!r}")
        pos = match.end()
        kind = match.lastgroup or ""
        value: Any = match.group()
        if kind == "ws":
            continue
        if kind == "num":
            value = float(value) if "." in value else int(value)
            kind = "value"
        elif kind == "str":
            quote = value[0]
            value = value[1:-1].replace(quote * 2, quote)
            kind = "value"
        elif kind == "ident":
            kind = "word"
        elif kind == "punct":
            kind = value
        tokens.append((kind, value))
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, Any]], columns: Sequence[str]) -> None:
        self._tokens = tokens
        self._columns = set(columns)
        self._i = 0

    def _peek(self) -> tuple[str, Any]:
        if self._i >= len(self._tokens):
            return ("", None)
        return self._tokens[self._i]

    def _next(self) -> tuple[str, Any]:
        token = self._tokens[self._i]
        self._i += 1
        return token

    def _is_word(self, word: str) -> bool:
        kind, value = self._peek()
        return kind == "word" and value.lower() == word

    def _expect_word(self, word: str) -> None:
        if not self._is_word(word):
            raise ValueError(f"Expected {word!r}, got {self._peek()!r}")
        self._next()

    def _expect(self, kind: str) -> None:
        actual, value = self._peek()
        if actual != kind:
            raise ValueError(f"Expected {kind!r}, got {value!r}")
        self._next()

    def parse(self) -> sql.Expr:
        """Разобрать всё выражение и убедиться, что токены кончились."""
        condition = self._parse_or()
        if self._i != len(self._tokens):
            raise ValueError(f"Unexpected token: {self._peek()!r}")
        return condition

    def _parse_or(self) -> sql.Expr:
        condition = self._parse_and()
        while self._is_word("or"):
            self._next()
            condition = sql.Or(condition, self._parse_and())
        return condition

    def _parse_and(self) -> sql.Expr:
        condition = self._parse_unary()
        while self._is_word("and"):
            self._next()
            condition = sql.And(condition, self._parse_unary())
        return condition

    def _parse_unary(self) -> sql.Expr:
        if self._is_word("not"):
            self._next()
            return sql.Not(self._parse_unary())
        kind, _ = self._peek()
        if kind == "(":
            self._next()
            condition = self._parse_or()
            self._expect(")")
            return condition
        return self._parse_predicate()

    def _parse_predicate(self) -> sql.Expr:
        kind, name = self._next()
        if kind != "word" or name.lower() in _RESERVED:
            raise ValueError(f"Expected column name, got {name!r}")
        if name not in self._columns:
            raise ValueError(f"Unknown column {name!r}")

        op_kind, op = self._peek()
        if op_kind == "op":
            self._next()
            value = self._parse_value()
            if value is None:
                return sql.IsNull(name, negated=op == "!=")
            return sql.Compare(name, op, value)
        if self._is_word("is"):
            self._next()
            negated = False
            if self._is_word("not"):
                self._next()
                negated = True
            self._expect_word("null")
            return sql.IsNull(name, negated=negated)
        if self._is_word("in"):
            self._next()
            return sql.InList(name, self._parse_list())
        if self._is_word("not"):
            self._next()
            self._expect_word("in")
            return sql.InList(name, self._parse_list(), negated=True)
        if self._is_word("like") or self._is_word("ilike"):
            insensitive = self._peek()[1].lower() == "ilike"
            self._next()
            return sql.Like(name, self._parse_value(), insensitive=insensitive)
        if self._is_word("between"):
            self._next()
            low = self._parse_value()
            self._expect_word("and")
            high = self._parse_value()
            return sql.Between(name, low, high)
        raise ValueError(f"Expected operator after column {name!r}, got {self._peek()!r}")

    def _parse_list(self) -> list[Any]:
        self._expect("(")
        values: list[Any] = []
        kind, _ = self._peek()
        if kind == ")":
            self._next()
            return values
        values.append(self._parse_value())
        while True:
            kind, _ = self._peek()
            if kind == ",":
                self._next()
                values.append(self._parse_value())
            elif kind == ")":
                self._next()
                return values
            else:
                raise ValueError(f"Expected ',' or ')', got {self._peek()!r}")

    def _parse_value(self) -> Any:
        kind, value = self._next()
        if kind == "value":
            return value
        if kind == "word":
            lowered = value.lower()
            if lowered == "true":
                return True
            if lowered == "false":
                return False
            if lowered == "null":
                return None
        raise ValueError(f"Expected value, got {value!r}")


def parse_where(expression: str, columns: Sequence[str]) -> sql.Expr:
    """Parse a ``WHERE`` mini-DSL string into an expression node.

    Поддерживает: ``and``/``or``/``not``, скобки, ``= != < <= > >=``,
    ``in (...)/not in (...)``, ``like``/``ilike``, ``is [not] null``,
    ``between ... and ...``. Имена колонок сверяются с ``columns``, значения
    становятся bound-параметрами; рендер в SQL — на стороне диалекта
    (:func:`ferronit.contrib.sql.compile_expr`).
    """
    return _Parser(_tokenize(expression), columns).parse()


class IdentityMap:
    """Instance cache mapping ``(model, primary_key)`` to a loaded instance.

    Lives for one unit of work: repeated point reads (``get``) of the same row
    return the same instance without a round-trip to the database. ``list`` does
    not populate it. The cache holds strong references and is meant to be
    discarded with the unit of work, so the unit's lifetime (not the garbage
    collector) bounds the memory it retains.
    """

    __slots__ = ("_map",)

    def __init__(self) -> None:
        self._map: dict[tuple[type[Any], Any], Any] = {}

    def get(self, model: type[Any], pk: Any) -> Any:
        """Return the cached instance for ``(model, pk)`` or ``None``."""
        return self._map.get((model, pk))

    def put(self, model: type[Any], pk: Any, obj: Any) -> None:
        """Cache ``obj`` under ``(model, pk)``."""
        self._map[(model, pk)] = obj


@lru_cache(maxsize=256)
def _repo_sql(
    model_cls: type[Model], table: str, pk: str, dialect: sql.Dialect = sql.POSTGRES
) -> tuple:
    """Pre-compute the connection-independent SQL for a (model, table, pk, dialect).

    These statements depend only on the model's columns, the table/primary-key
    names and the engine, so they are rendered once by the SQL node tree and reused
    by every repository instance — which keeps the hot read path free of per-request
    string formatting.

    Values are bound at call time, so only the SQL text of the compiled statements
    is used here. The key comparison of the point read is therefore a
    placeholder-only :class:`~ferronit.contrib.sql.Raw` fragment: the node tree
    models filter *values*, and this statement has none until it is executed.

    ``RETURNING`` is added only where the engine supports it: on MySQL the insert
    and upsert statements come back without it, and the repository reads the
    generated key from the driver instead.
    """
    columns = model_cls.__columns__
    select_columns = model_cls.__select_columns__
    insert_columns = tuple(c for c in columns if c != pk)
    returning = columns if dialect.supports_returning else ()
    insert_sql = sql.compile_statement(
        sql.Insert(table, insert_columns, returning=returning), dialect
    )[0]
    upsert_sql = sql.compile_statement(
        sql.Insert(
            table,
            columns,
            conflict_key=pk,
            update_columns=insert_columns,
            returning=returning,
        ),
        dialect,
    )[0]
    select_pk_sql = sql.compile_statement(
        sql.Select(table, columns, where=[sql.Raw(f"{pk} = $1")]), dialect
    )[0]
    delete_sql = sql.compile_statement(sql.Delete(table, pk), dialect)[0]
    list_sql = sql.compile_statement(
        sql.Select(table, columns, limit=1, offset=2), dialect
    )[0]
    return (
        table,
        columns,
        select_columns,
        insert_columns,
        ", ".join(insert_columns),
        insert_sql,
        upsert_sql,
        select_pk_sql,
        delete_sql,
        list_sql,
    )


@lru_cache(maxsize=256)
def _join_select(parent_sql: str, model: type[Model]) -> str:
    """Обернуть SELECT по родителю в LEFT JOIN всех объявленных связей.

    Родительский SELECT (с фильтрами, сортировкой и пагинацией) уходит в
    подзапрос ``t0``, чтобы его голые имена колонок оставались однозначными;
    снаружи колонки квалифицируются алиасами ``t0``/``t1``/…, и связи
    джойнятся по FK дочерней таблицы к PK родителя. Параметры родительского
    запроса не меняются — JOIN добавляет только имена колонок.
    """
    relations = model.__relations__
    if not relations:
        return parent_sql
    cols = [f"t0.{c}" for c in model.__columns__]
    joins: list[str] = []
    for index, (child, fk, pk, _many) in enumerate(relations.values(), start=1):
        alias = f"t{index}"
        cols.extend(f"{alias}.{c}" for c in child.__columns__)
        joins.append(
            f" LEFT JOIN {_resolve_table(child)} {alias} ON {alias}.{fk} = t0.{pk}"
        )
    return f"SELECT {', '.join(cols)} FROM ({parent_sql}) t0{''.join(joins)}"


class RawModelRepository[M: Model](Adapter):
    """Repository over one table returning model instances instead of dicts.

    The engine comes from the connection: an ``asyncpg`` connection is PostgreSQL
    (``$1``, ``RETURNING``), the adapters in :mod:`ferronit.contrib.drivers` are
    MySQL or SQLite. Where the engine has no ``RETURNING`` (MySQL) the generated
    key is taken from the driver and the row is read back with a point select;
    that is one extra round-trip per write, and only for such engines.

    The primary key is excluded from ``INSERT`` (the server generates it) and
    ``SELECT``/``DELETE`` key off it. Column order comes from the model, so every
    emitted SQL string is stable — which lets the driver reuse its prepared
    statement for the statement cache.

    Usage:
        repo = RawModelRepository(connection, User)
        user = await repo.get(1)                    # → User | None
        users = await repo.list(Condition("age > $1", [18]), limit=50)
        saved = await repo.save(User(name="A"))     # → User with server id
    """

    __slots__ = (
        "_columns",
        "_conn",
        "_delete_sql",
        "_dialect",
        "_identity_map",
        "_insert_cols",
        "_insert_columns",
        "_insert_sql",
        "_list_sql",
        "_model",
        "_pk",
        "_select_columns",
        "_select_pk_sql",
        "_table",
        "_upsert_sql",
    )

    def __init__(
        self,
        connection: Any,
        model_cls: type[M],
        table: str | None = None,
        pk: str = "id",
        identity_map: IdentityMap | None = None,
    ) -> None:
        self._conn = connection
        self._model = model_cls
        self._pk = pk
        self._identity_map = identity_map
        # Диалект берём у соединения; у asyncpg атрибута нет — значит PostgreSQL.
        self._dialect = getattr(connection, "dialect", sql.POSTGRES)
        (
            self._table, self._columns, self._select_columns, self._insert_columns,
            self._insert_cols, self._insert_sql, self._upsert_sql,
            self._select_pk_sql, self._delete_sql, self._list_sql,
        ) = _repo_sql(
            model_cls, table or _resolve_table(model_cls), pk, self._dialect
        )

    async def _materialize(self, rows) -> list[M]:
        """Построить модели из строк, подтянув связи отдельным запросом.

        Каждая связь грузится одним ``SELECT … WHERE fk = ANY($1)`` на всех
        родителей сразу, поэтому N+1 не возникает. Это путь массового чтения
        (``list``/``fetch``): отдельный запрос не дублирует родительские
        колонки и на больших наборах быстрее ``LEFT JOIN``
        (см. :meth:`_materialize_joined`).
        """
        if not rows:
            return []
        relations = self._model.__relations__
        if not relations:
            return [self._model(*row) for row in rows]
        pk_index = self._columns.index(self._pk)
        pks = [row[pk_index] for row in rows]
        rel_data: dict[str, tuple[dict[Any, list], bool]] = {}
        for field, (child, fk, _pk, many) in relations.items():
            if self._dialect.supports_any:
                # PostgreSQL: весь набор ключей уходит одним параметром-списком.
                child_rows = await self._conn.fetch(
                    sql.compile_statement(
                        sql.Select(
                            _resolve_table(child),
                            child.__columns__,
                            where=[sql.Raw(f"{fk} = ANY($1)")],
                        ),
                        self._dialect,
                    )[0],
                    pks,
                )
            else:
                # MySQL/SQLite: ANY нет — набор разворачивается в IN (%s, %s, …).
                child_sql, child_params = sql.compile_statement(
                    sql.Select(
                        _resolve_table(child),
                        child.__columns__,
                        where=[sql.InList(fk, pks)],
                    ),
                    self._dialect,
                )
                child_rows = await self._conn.fetch(child_sql, *child_params)
            fk_index = child.__columns__.index(fk)
            grouped: dict[Any, list] = {}
            for crow in child_rows:
                grouped.setdefault(crow[fk_index], []).append(child(*crow))
            rel_data[field] = (grouped, many)
        models = []
        for row in rows:
            kwargs = {c: row[i] for i, c in enumerate(self._columns)}
            pk_val = kwargs[self._pk]
            for field, (grouped, many) in rel_data.items():
                if many:
                    kwargs[field] = grouped.get(pk_val, [])
                else:
                    items = grouped.get(pk_val)
                    kwargs[field] = items[0] if items else None
            models.append(self._model(**kwargs))
        return models

    async def _materialize_joined(self, rows) -> list[M]:
        """Построить модели из плоских JOIN-строк (точечное чтение связей).

        Связи грузятся одним ``LEFT JOIN`` (см. ``_join_select``), поэтому
        строка содержит колонки родителя, затем колонки каждой связи в порядке
        объявления. Повторяющиеся дочерние строки (перекрёстное произведение
        нескольких коллекций) отсеиваются по кортежу значений; строка без
        детей приходит с ``NULL``-ами в дочерних колонках.
        """
        if not rows:
            return []
        relations = self._model.__relations__
        if not relations:
            return [self._model(*row) for row in rows]

        parent_cols = self._columns
        n_parent = len(parent_cols)
        pk_index = parent_cols.index(self._pk)

        rel_fields = list(relations)
        rel_specs = [relations[field] for field in rel_fields]

        offsets: list[int] = []
        child_widths: list[int] = []
        offset = n_parent
        for child, _fk, _pk, _many in rel_specs:
            offsets.append(offset)
            child_widths.append(len(child.__columns__))
            offset += child_widths[-1]

        order: list[Any] = []
        grouped: dict[Any, tuple[tuple[Any, ...], list[Any]]] = {}
        for row in rows:
            pk = row[pk_index]
            if pk not in grouped:
                accs: list[Any] = []
                for _child, _fk, _pk, many in rel_specs:
                    accs.append((set(), [] if many else None))
                grouped[pk] = (tuple(row[:n_parent]), accs)
                order.append(pk)
            accs = grouped[pk][1]
            for i, (child, _fk, _pk, many) in enumerate(rel_specs):
                vals = tuple(row[offsets[i]:offsets[i] + child_widths[i]])
                if all(value is None for value in vals):
                    continue  # LEFT JOIN без детей — пропускаем
                seen, acc = accs[i]
                if many:
                    if vals not in seen:
                        seen.add(vals)
                        acc.append(child(*vals))
                elif acc is None:
                    acc = child(*vals)
                accs[i] = (seen, acc)

        models: list[M] = []
        for pk in order:
            parent_vals, accs = grouped[pk]
            kwargs = {c: parent_vals[i] for i, c in enumerate(parent_cols)}
            for field, (_seen, acc) in zip(rel_fields, accs, strict=True):
                kwargs[field] = acc
            models.append(self._model(**kwargs))
        return models

    def _materialize_joined_pairs(
        self, rows, joins: list[tuple[type[Model], Any, Any, Any]]
    ) -> builtins.list[builtins.tuple[Any, ...]]:
        """Разобрать плоские JOIN-строки в кортежи ``(родитель, [дети]…)``.

        Строки приходят из явного ``Query.join`` (см. ``_wrap_joins``): колонки
        родителя, затем колонки каждой джойн-модели по порядку объявления.
        Повторяющиеся дочерние строки (перекрёстное произведение нескольких
        джойнов) отсеиваются по кортежу значений.
        """
        if not rows:
            return []
        parent_cols = self._columns
        n_parent = len(parent_cols)
        pk_index = parent_cols.index(self._pk)

        child_models = [child for child, _on, _fk, _pk in joins]
        offsets: list[int] = []
        widths: list[int] = []
        offset = n_parent
        for child in child_models:
            offsets.append(offset)
            widths.append(len(child.__columns__))
            offset += widths[-1]

        n_joins = len(child_models)
        order: list[Any] = []
        grouped: dict[Any, tuple[tuple[Any, ...], list[Any]]] = {}
        for row in rows:
            pk = row[pk_index]
            if pk not in grouped:
                grouped[pk] = (tuple(row[:n_parent]), [(set(), []) for _ in range(n_joins)])
                order.append(pk)
            _parent_vals, accs = grouped[pk]
            for i, child in enumerate(child_models):
                vals = tuple(row[offsets[i]:offsets[i] + widths[i]])
                if all(value is None for value in vals):
                    continue
                seen, acc = accs[i]
                if vals not in seen:
                    seen.add(vals)
                    acc.append(child(*vals))

        result: list[tuple[Any, ...]] = []
        for pk in order:
            parent_vals, accs = grouped[pk]
            children = tuple(acc for _seen, acc in accs)
            result.append((self._model(*parent_vals), *children))
        return result

    async def _load(self, id: Any) -> M | None:
        """Read one row (with its relations) straight from the database.

        No identity-map involvement: the map lives for one unit of work and would
        hand back a stale copy right after a write.

        Args:
            id: Primary key value.

        Returns:
            The model instance, or ``None`` when no row matches.
        """
        if self._model.__relations__:
            rows = await self._conn.fetch(_join_select(self._select_pk_sql, self._model), id)
            if not rows:
                return None
            return (await self._materialize_joined(rows))[0]
        row = await self._conn.fetchrow(self._select_pk_sql, id)
        return self._model(*row) if row is not None else None

    async def get(self, id: Any) -> M | None:
        """Fetch a single row by primary key.

        Args:
            id: Primary key value.

        Returns:
            A model instance, or ``None`` when nothing matches.
        """
        if self._identity_map is not None:
            cached = self._identity_map.get(self._model, id)
            if cached is not None:
                return cached
        obj = await self._load(id)
        if obj is not None and self._identity_map is not None:
            self._identity_map.put(self._model, id, obj)
        return obj

    async def list(
        self, *filters: Condition, limit: int = 100, offset: int = 0
    ) -> list[M]:
        """List rows, optionally filtered and paginated.

        Args:
            *filters: Conditions joined with ``AND``.
            limit: Maximum number of rows to return.
            offset: Number of rows to skip.

        Returns:
            A list of model instances (possibly empty).
        """
        if filters:
            sql_text, params = sql.compile_statement(
                sql.Select(
                    self._table,
                    self._columns,
                    where=[sql.Paren(f) for f in filters],
                    limit=limit,
                    offset=offset,
                ),
                self._dialect,
            )
            rows = await self._conn.fetch(sql_text, *params)
        else:
            rows = await self._conn.fetch(self._list_sql, limit, offset)
        # Identity-map наполняется только из get(): на list() карта почти никогда
        # не пригождается, а класть каждую строку в неё — лишние ~50 µs на 1000 строк.
        return await self._materialize(rows)

    async def fetch(self, query: Query) -> builtins.list[M]:
        """Run a compiled ``Query`` and return model instances.

        Args:
            query: A ``Query`` (or anything with a ``compile()`` returning
                ``(sql, params)``).

        Returns:
            A list of model instances.
        """
        sql_text, params = query.compile(self._dialect)
        rows = await self._conn.fetch(sql_text, *params)
        return await self._materialize(rows)

    async def fetch_one(self, query: Query) -> M | None:
        """Fetch the first matching row, or ``None``.

        Adds ``LIMIT 1`` when the query has no limit yet, so the database does
        not materialise the whole result set.
        """
        if query._limit is None:
            query = query.limit(1)
        sql_text, params = query.compile(self._dialect)
        if self._model.__relations__:
            rows = await self._conn.fetch(_join_select(sql_text, self._model), *params)
            if not rows:
                return None
            return (await self._materialize_joined(rows))[0]
        row = await self._conn.fetchrow(sql_text, *params)
        if row is None:
            return None
        return self._model(*row)

    async def fetch_joined(
        self, query: Query
    ) -> builtins.list[builtins.tuple[Any, ...]]:
        """Run a ``Query`` with explicit ``join``-s and return ``(parent, [children]…)``.

        Each tuple is ``(parent, children_of_join_1, children_of_join_2, …)``,
        one list per ``join``. The parent columns come from this repository's
        model; joined models arrive in declaration order.

        Args:
            query: A ``Query`` built with ``.join(...)``.

        Returns:
            A list of ``(parent, [children]…)`` tuples.
        """
        sql_text, params = query.compile(self._dialect)
        rows = await self._conn.fetch(sql_text, *params)
        return self._materialize_joined_pairs(rows, query._joins)

    async def fetch_value(self, query: Query) -> Any | None:
        """Fetch the first column of the first row — for ``count()``/``aggregate()``."""
        sql_text, params = query.compile(self._dialect)
        row = await self._conn.fetchrow(sql_text, *params)
        return row[0] if row is not None else None

    async def fetch_rows(self, query: Query) -> builtins.list[Any]:
        """Run a ``Query`` and return raw rows — for projections and aggregates.

        Grouped/aggregated results are not model instances, so the rows come back as
        the driver gives them: an ``asyncpg`` record, or
        :class:`~ferronit.contrib.drivers.Row` (position *and* name access) on
        MySQL/SQLite. Use ``count()``/``aggregate()`` with ``fetch_value()`` when a
        single number is all that is needed.

        Args:
            query: A ``Query`` built with ``columns()``/``group_by()``/``having()``.

        Returns:
            The rows of the compiled query.
        """
        sql_text, params = query.compile(self._dialect)
        return builtins.list(await self._conn.fetch(sql_text, *params))

    async def save(self, obj: M) -> M:
        """Insert a row and return the full server-side instance.

        On engines with ``RETURNING`` the row comes back in the same round-trip; on
        MySQL the generated key comes from the driver and the row is read back with
        a point select.

        Args:
            obj: The model instance to insert (primary key ignored).

        Returns:
            The inserted row as a model instance (including the generated key).
        """
        values = tuple(getattr(obj, name) for name in self._insert_columns)
        if self._dialect.supports_returning:
            row = await self._conn.fetchrow(self._insert_sql, *values)
            return self._model(*row) if row is not None else obj
        key = await self._conn.execute(self._insert_sql, *values)
        fetched = await self._load(key) if key else None
        if fetched is not None:
            if self._identity_map is not None:
                self._identity_map.put(self._model, key, fetched)
            return fetched
        return msgspec.structs.replace(obj, **{self._pk: key})

    async def upsert(self, obj: M) -> M:
        """Insert or replace a row by primary key (client-provided identity).

        Unlike :meth:`save`, the primary key is part of the ``INSERT``; on a
        key conflict every non-key column is overwritten from the new row. On
        MySQL the refresh is read back by that key (``lastrowid`` is 0 when the
        statement updated an existing row instead of inserting).
        """
        values = tuple(getattr(obj, name) for name in self._columns)
        if self._dialect.supports_returning:
            row = await self._conn.fetchrow(self._upsert_sql, *values)
            return self._model(*row) if row is not None else obj
        await self._conn.execute(self._upsert_sql, *values)
        key = getattr(obj, self._pk)
        fetched = await self._load(key) if key is not None else None
        if fetched is not None:
            if self._identity_map is not None:
                self._identity_map.put(self._model, key, fetched)
            return fetched
        return obj

    async def save_many(self, objs: Sequence[M]) -> builtins.list[M]:
        """Bulk-insert rows and return the server-side rows.

        PostgreSQL and SQLite insert every row in one statement and read the rows
        back with ``RETURNING``. MySQL has no ``RETURNING``, so the rows go in one by
        one and the key of each comes from ``lastrowid`` — N round-trips instead of
        one statement. That is the engine's limitation, not the generator's: the
        single-statement form is what the dialect cannot express.
        """
        if not objs:
            return []
        if self._dialect.supports_returning:
            sql_text = sql.compile_statement(
                sql.Insert(
                    self._table,
                    self._insert_columns,
                    rows=len(objs),
                    returning=self._columns,
                ),
                self._dialect,
            )[0]
            values: builtins.list[Any] = []
            for obj in objs:
                values.extend(getattr(obj, name) for name in self._insert_columns)
            rows = await self._conn.fetch(sql_text, *values)
            return [self._model(*row) for row in rows]
        row_sql = sql.compile_statement(
            sql.Insert(self._table, self._insert_columns), self._dialect
        )[0]
        saved: builtins.list[M] = []
        for obj in objs:
            row_values = tuple(getattr(obj, name) for name in self._insert_columns)
            key = await self._conn.execute(row_sql, *row_values)
            saved.append(msgspec.structs.replace(obj, **{self._pk: key}))
        return saved

    async def delete_by(self, column: str, value: Any) -> None:
        """Delete rows where ``column = value`` (child-table sync for aggregates)."""
        await self._conn.execute(
            sql.compile_statement(sql.Delete(self._table, column), self._dialect)[0],
            value,
        )

    async def update(self, id: Any, data: dict[str, Any]) -> M | None:
        """Update a row by primary key and return the fresh model instance.

        Args:
            id: Primary key value of the row to update.
            data: Column name → new value mapping; an empty mapping is a no-op.

        Returns:
            The updated row as a model instance, or ``None`` if no row matches.
        """
        if not data:
            return await self.get(id)
        if not self._dialect.supports_returning:
            sql_text = sql.compile_statement(
                sql.Update(self._table, list(data), self._pk), self._dialect
            )[0]
            await self._conn.execute(sql_text, *data.values(), id)
            fresh = await self._load(id)
            if fresh is not None and self._identity_map is not None:
                self._identity_map.put(self._model, id, fresh)
            return fresh
        sql_text = sql.compile_statement(
            sql.Update(self._table, list(data), self._pk, returning=self._columns),
            self._dialect,
        )[0]
        row = await self._conn.fetchrow(sql_text, *data.values(), id)
        if row is None:
            return None
        obj = self._model.from_row(row)
        if self._identity_map is not None:
            self._identity_map.put(self._model, id, obj)
        return obj

    async def delete(self, id: Any) -> None:
        """Delete a row by primary key.

        Args:
            id: Primary key value of the row to delete.
        """
        await self._conn.execute(self._delete_sql, id)




class Mapper[A, M]:
    """Перевод «агрегат ↔ модель строки» (адаптерный слой гексагона).

    Модель строки остаётся тупой схемой таблицы; весь маппинг домена
    сосредоточен в одном маппере. Переопредели оба метода.
    """

    def to_aggregate(self, model: M) -> A:
        """Собрать доменный агрегат из модели строки (со всеми связями)."""
        raise NotImplementedError

    def from_aggregate(self, aggregate: A) -> M:
        """Разложить агрегат в модель строки (+ дочерние модели)."""
        raise NotImplementedError


class Repository[A, M: Model, MAP: Mapper[Any, Any], P: Pool]:
    """Агрегатный репозиторий — порт над агрегатом, а не над таблицей.

    ``Repository[Order, OrderModel, OrderMapper, ShopPool]``: агрегат, модель
    строки, маппер и тип пула — всё из generic (``__init_subclass__``).

    Example:
        class OrderMapper(Mapper[Order, OrderModel]):
            def to_aggregate(self, m): ...
            def from_aggregate(self, a): ...

        class OrderRepository(Repository[Order, OrderModel, OrderMapper, ShopPool]):
            async def by_user(self, user_id):
                return await self.list(OrderModel.c.user_id == user_id)

    ``save`` кладёт агрегат целиком (header + дочерние таблицы) в одну
    транзакцию; ``get``/``list`` возвращают агрегаты со всеми связями.
    """

    aggregate: type[A]
    model: type[M]
    mapper_cls: type[MAP]
    pool: P
    _mapper: Mapper[A, M]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for base in getattr(cls, "__orig_bases__", ()):
            if get_origin(base) is Repository:
                args = get_args(base)
                if len(args) != 4:
                    raise TypeError(
                        f"{cls.__name__}: Repository[A, M, MAP, P] — укажи все 4 "
                        f"параметра (агрегат, модель, маппер, пул), получено {len(args)}"
                    )
                cls.aggregate = args[0]
                cls.model = args[1]
                cls.mapper_cls = args[2]
                cls.pool = args[3]

    def __init__(self, pool: Pool) -> None:
        self._pool = pool
        self._mapper = self.mapper_cls()

    def uow(self, *, readonly: bool = False) -> RawUnitOfWork:
        """Открыть UoW для сценария из нескольких операций."""
        return self._pool.uow(readonly=readonly)

    async def get(self, id: Any) -> A | None:
        """Загрузить агрегат по id (со всеми объявленными связями)."""
        async with self._pool.uow(readonly=True) as uow:
            model = await uow.model(self.model).get(id)
        return self._mapper.to_aggregate(model) if model is not None else None

    async def list(
        self, *filters: Condition, limit: int = 100, offset: int = 0
    ) -> builtins.list[A]:
        """Список агрегатов, опционально с фильтром и пагинацией."""
        async with self._pool.uow(readonly=True) as uow:
            rows = await uow.model(self.model).list(*filters, limit=limit, offset=offset)
        return [self._mapper.to_aggregate(m) for m in rows]

    async def save(self, aggregate: A) -> A:
        """Сохранить агрегат целиком: header (upsert) + дочерние в одной UoW."""
        model = self._mapper.from_aggregate(aggregate)
        async with self._pool.uow() as uow:
            repo = uow.model(self.model)
            saved = await repo.upsert(model)
            for field, (child, fk, pk, _many) in self.model.__relations__.items():
                parent_pk = getattr(saved, pk)
                await uow.model(child).delete_by(fk, parent_pk)
                children = getattr(model, field)
                if children:
                    fixed = [
                        msgspec.structs.replace(c, **{fk: parent_pk}) for c in children
                    ]
                    await uow.model(child).save_many(fixed)
        return aggregate

    async def delete(self, id: Any) -> None:
        """Удалить агрегат: дочерние таблицы по FK, затем header."""
        async with self._pool.uow() as uow:
            for child, fk, _pk, _many in self.model.__relations__.values():
                await uow.model(child).delete_by(fk, id)
            await uow.model(self.model).delete(id)
