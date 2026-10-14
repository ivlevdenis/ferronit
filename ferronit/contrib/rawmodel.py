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

import asyncpg
import msgspec
from msgspec.structs import StructMeta  # type: ignore[attr-defined]
from msgspec.structs import fields as struct_fields

from ferronit.contrib.rawdb import Condition, Pool, RawUnitOfWork, _merge_filters
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
    """Typed column reference; comparison operators produce ``Condition`` filters.

    Columns are reached through ``Model.c`` (e.g. ``User.c.age``), so field names
    stay type-checked at authoring time instead of being raw SQL strings.
    Comparing two columns (``A.c.id == B.c.a_id``) yields an :class:`OnPair` for
    an explicit ``Query.join``.
    """

    __slots__ = ("_model", "name")

    def __init__(self, name: str, model: type[Model] | None = None) -> None:
        self.name = name
        self._model = model

    def __gt__(self, other: Any) -> Condition:
        return Condition(f"{self.name} > $1", [other])

    def __ge__(self, other: Any) -> Condition:
        return Condition(f"{self.name} >= $1", [other])

    def __lt__(self, other: Any) -> Condition:
        return Condition(f"{self.name} < $1", [other])

    def __le__(self, other: Any) -> Condition:
        return Condition(f"{self.name} <= $1", [other])

    def __eq__(self, other: Any) -> Condition | OnPair:  # type: ignore[override]
        if other is None:
            return Condition(f"{self.name} IS NULL", [])
        if isinstance(other, Column):
            return OnPair(self, other)
        return Condition(f"{self.name} = $1", [other])

    def __ne__(self, other: Any) -> Condition:  # type: ignore[override]
        if other is None:
            return Condition(f"{self.name} IS NOT NULL", [])
        return Condition(f"{self.name} <> $1", [other])

    def in_(self, values: Sequence[Any]) -> Condition:
        """``IN``: поле входит в набор значений (``col = ANY($1)``)."""
        return Condition(f"{self.name} = ANY($1)", [list(values)])

    def not_in(self, values: Sequence[Any]) -> Condition:
        """``NOT IN``: поле не входит в набор значений."""
        return Condition(f"NOT ({self.name} = ANY($1))", [list(values)])

    def like(self, pattern: str) -> Condition:
        """``LIKE`` с wildcard-паттерном (``%``/``_``)."""
        return Condition(f"{self.name} LIKE $1", [pattern])

    def ilike(self, pattern: str) -> Condition:
        """``ILIKE`` — LIKE без учёта регистра."""
        return Condition(f"{self.name} ILIKE $1", [pattern])

    def is_null(self) -> Condition:
        """``IS NULL``."""
        return Condition(f"{self.name} IS NULL", [])

    def is_not_null(self) -> Condition:
        """``IS NOT NULL``."""
        return Condition(f"{self.name} IS NOT NULL", [])

    def between(self, low: Any, high: Any) -> Condition:
        """``col BETWEEN $1 AND $2``."""
        return Condition(f"{self.name} BETWEEN $1 AND $2", [low, high])

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
    """A ``SELECT`` over one model, compiled to stable SQL with bound parameters.

    Usage:
        q = select(User).where(User.c.age > 18).order_by(User.c.name).limit(50)
        rows = await repo.fetch(q)
    """

    __slots__ = (
        "_joins",
        "_limit",
        "_model",
        "_offset",
        "_order",
        "_select",
        "_table",
        "_where",
    )

    def __init__(self, model: type[Model], table: str | None = None) -> None:
        self._model = model
        self._table = table or _resolve_table(model)
        # Предкомпиляция статики: колонки известны на уровне класса.
        self._select = f"SELECT {model.__select_columns__} FROM {self._table}"
        self._where: list[Condition] = []
        self._order: list[str] = []
        self._limit: int | None = None
        self._offset: int | None = None
        self._joins: list[tuple[type[Model], OnPair | None, str | None, str]] = []

    def where(self, *conditions: Condition | str) -> Query:
        """Add ``AND``-combined filter conditions.

        Условие может быть готовым :class:`Condition` (сравнения через ``User.c.*``,
        ``|``/``&``/``~``) или строкой мини-DSL — она парсится и параметры биндятся:

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

    def columns(self, *names: str) -> Query:
        """Выбрать конкретные колонки вместо всех полей модели."""
        self._select = f"SELECT {', '.join(names)} FROM {self._table}"
        return self

    def count(self) -> Query:
        """Агрегат ``count(*)``; результат забирается через ``repo.fetch_value()``."""
        self._select = f"SELECT count(*) FROM {self._table}"
        return self

    def aggregate(self, expression: str) -> Query:
        """Произвольный агрегат (``sum(col)``, ``avg(col)``, ...); через ``fetch_value()``."""
        self._select = f"SELECT {expression} FROM {self._table}"
        return self

    def compile(self) -> tuple[str, list[Any]]:
        """Compile to ``(sql, params)`` with continuous ``$N`` placeholders."""
        sql = self._select
        where, params = _merge_filters(self._where)
        if where:
            sql += f" WHERE {where}"
        if self._order:
            sql += " ORDER BY " + ", ".join(self._order)
        if self._limit is not None:
            params.append(self._limit)
            sql += f" LIMIT ${len(params)}"
        if self._offset is not None:
            params.append(self._offset)
            sql += f" OFFSET ${len(params)}"
        if self._joins:
            sql, params = _wrap_joins(sql, params, self._model, self._joins)
        return sql, params


def select(model: type[Model], table: str | None = None) -> Query:
    """Start a ``SELECT`` over ``model``."""
    return Query(model, table)


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

    def parse(self) -> Condition:
        condition = self._parse_or()
        if self._i != len(self._tokens):
            raise ValueError(f"Unexpected token: {self._peek()!r}")
        return condition

    def _parse_or(self) -> Condition:
        condition = self._parse_and()
        while self._is_word("or"):
            self._next()
            condition = condition | self._parse_and()
        return condition

    def _parse_and(self) -> Condition:
        condition = self._parse_unary()
        while self._is_word("and"):
            self._next()
            condition = condition & self._parse_unary()
        return condition

    def _parse_unary(self) -> Condition:
        if self._is_word("not"):
            self._next()
            return ~self._parse_unary()
        kind, _ = self._peek()
        if kind == "(":
            self._next()
            condition = self._parse_or()
            self._expect(")")
            return condition
        return self._parse_predicate()

    def _parse_predicate(self) -> Condition:
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
                return Condition(f"{name} IS {'NOT ' if op == '!=' else ''}NULL", [])
            sql_op = "<>" if op == "!=" else op
            return Condition(f"{name} {sql_op} $1", [value])
        if self._is_word("is"):
            self._next()
            negated = False
            if self._is_word("not"):
                self._next()
                negated = True
            self._expect_word("null")
            return Condition(f"{name} IS {'NOT ' if negated else ''}NULL", [])
        if self._is_word("in"):
            self._next()
            values = self._parse_list()
            return Condition(f"{name} = ANY($1)", [values])
        if self._is_word("not"):
            self._next()
            self._expect_word("in")
            values = self._parse_list()
            return Condition(f"NOT ({name} = ANY($1))", [values])
        if self._is_word("like") or self._is_word("ilike"):
            op = "ILIKE" if self._peek()[1].lower() == "ilike" else "LIKE"
            self._next()
            value = self._parse_value()
            return Condition(f"{name} {op} $1", [value])
        if self._is_word("between"):
            self._next()
            low = self._parse_value()
            self._expect_word("and")
            high = self._parse_value()
            return Condition(f"{name} BETWEEN $1 AND $2", [low, high])
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


def parse_where(expression: str, columns: Sequence[str]) -> Condition:
    """Parse a ``WHERE`` mini-DSL string into a :class:`Condition`.

    Поддерживает: ``and``/``or``/``not``, скобки, ``= != < <= > >=``,
    ``in (...)/not in (...)``, ``like``/``ilike``, ``is [not] null``,
    ``between ... and ...``. Имена колонок сверяются с ``columns``.
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
def _repo_sql(model_cls: type[Model], table: str, pk: str) -> tuple:
    """Pre-compute the connection-independent SQL for ``(model, table, pk)``.

    These strings depend only on the model's columns and the table/primary-key
    names, so they are built once and reused by every repository instance —
    which keeps the hot read path free of per-request string formatting.
    """
    columns = model_cls.__columns__
    select_columns = model_cls.__select_columns__
    insert_columns = tuple(c for c in columns if c != pk)
    insert_cols = ", ".join(insert_columns)
    placeholders = ", ".join(f"${i}" for i in range(1, len(insert_columns) + 1))
    insert_sql = (
        f"INSERT INTO {table} ({insert_cols}) VALUES ({placeholders})"
        f" RETURNING {select_columns}"
    )
    all_cols = ", ".join(columns)
    all_placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
    update_set = ", ".join(f"{c} = EXCLUDED.{c}" for c in insert_columns)
    upsert_sql = (
        f"INSERT INTO {table} ({all_cols}) VALUES ({all_placeholders})"
        f" ON CONFLICT ({pk}) DO UPDATE SET {update_set}"
        f" RETURNING {select_columns}"
    )
    select_pk_sql = f"SELECT {select_columns} FROM {table} WHERE {pk} = $1"
    delete_sql = f"DELETE FROM {table} WHERE {pk} = $1"
    list_sql = f"SELECT {select_columns} FROM {table} LIMIT $1 OFFSET $2"
    return (
        table, columns, select_columns, insert_columns, insert_cols,
        insert_sql, upsert_sql, select_pk_sql, delete_sql, list_sql,
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

    The primary key is excluded from ``INSERT`` (the server generates it) and is
    read back with ``RETURNING``; ``SELECT``/``DELETE`` key off it. Column order
    comes from the model, so every emitted SQL string is stable — which lets
    asyncpg reuse its prepared statement for the statement cache.

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
        connection: asyncpg.Connection,
        model_cls: type[M],
        table: str | None = None,
        pk: str = "id",
        identity_map: IdentityMap | None = None,
    ) -> None:
        self._conn = connection
        self._model = model_cls
        self._pk = pk
        self._identity_map = identity_map
        (
            self._table, self._columns, self._select_columns, self._insert_columns,
            self._insert_cols, self._insert_sql, self._upsert_sql,
            self._select_pk_sql, self._delete_sql, self._list_sql,
        ) = _repo_sql(model_cls, table or _resolve_table(model_cls), pk)

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
            child_rows = await self._conn.fetch(
                f"SELECT {child.__select_columns__} FROM {_resolve_table(child)}"
                f" WHERE {fk} = ANY($1)",
                pks,
            )
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
        if self._model.__relations__:
            rows = await self._conn.fetch(_join_select(self._select_pk_sql, self._model), id)
            if not rows:
                return None
            obj = (await self._materialize_joined(rows))[0]
        else:
            row = await self._conn.fetchrow(self._select_pk_sql, id)
            if row is None:
                return None
            obj = self._model(*row)
        if self._identity_map is not None:
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
            where, params = _merge_filters(filters)
            clause = f" WHERE {where}"
            sql = (
                f"SELECT {self._select_columns} FROM {self._table}{clause}"
                f" LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
            )
            rows = await self._conn.fetch(sql, *params, limit, offset)
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
        sql, params = query.compile()
        rows = await self._conn.fetch(sql, *params)
        return await self._materialize(rows)

    async def fetch_one(self, query: Query) -> M | None:
        """Fetch the first matching row, or ``None``.

        Adds ``LIMIT 1`` when the query has no limit yet, so the database does
        not materialise the whole result set.
        """
        if query._limit is None:
            query = query.limit(1)
        sql, params = query.compile()
        if self._model.__relations__:
            rows = await self._conn.fetch(_join_select(sql, self._model), *params)
            if not rows:
                return None
            return (await self._materialize_joined(rows))[0]
        row = await self._conn.fetchrow(sql, *params)
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
        sql, params = query.compile()
        rows = await self._conn.fetch(sql, *params)
        return self._materialize_joined_pairs(rows, query._joins)

    async def fetch_value(self, query: Query) -> Any | None:
        """Fetch the first column of the first row — for ``count()``/``aggregate()``."""
        sql, params = query.compile()
        row = await self._conn.fetchrow(sql, *params)
        return row[0] if row is not None else None

    async def save(self, obj: M) -> M:
        """Insert a row and return the full server-side instance.

        Args:
            obj: The model instance to insert (primary key ignored).

        Returns:
            The inserted row as a model instance (including the generated key).
        """
        values = tuple(getattr(obj, name) for name in self._insert_columns)
        row = await self._conn.fetchrow(self._insert_sql, *values)
        return self._model(*row) if row is not None else obj

    async def upsert(self, obj: M) -> M:
        """Insert or replace a row by primary key (client-provided identity).

        Unlike :meth:`save`, the primary key is part of the ``INSERT``; on a
        key conflict every non-key column is overwritten from the new row.
        """
        values = tuple(getattr(obj, name) for name in self._columns)
        row = await self._conn.fetchrow(self._upsert_sql, *values)
        return self._model(*row) if row is not None else obj

    async def save_many(self, objs: Sequence[M]) -> builtins.list[M]:
        """Bulk-insert rows in one statement and return the server-side rows."""
        if not objs:
            return []
        n = len(self._insert_columns)
        groups: builtins.list[str] = []
        values: builtins.list[Any] = []
        for k, obj in enumerate(objs):
            start = k * n
            groups.append(
                "(" + ", ".join(f"${start + i}" for i in range(1, n + 1)) + ")"
            )
            values.extend(getattr(obj, name) for name in self._insert_columns)
        sql = (
            f"INSERT INTO {self._table} ({self._insert_cols})"
            f" VALUES {', '.join(groups)} RETURNING {self._select_columns}"
        )
        rows = await self._conn.fetch(sql, *values)
        return [self._model(*row) for row in rows]

    async def delete_by(self, column: str, value: Any) -> None:
        """Delete rows where ``column = value`` (child-table sync for aggregates)."""
        await self._conn.execute(
            f"DELETE FROM {self._table} WHERE {column} = $1", value
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
        assignments = ", ".join(f"{c} = ${i}" for i, c in enumerate(data, 1))
        row = await self._conn.fetchrow(
            f"UPDATE {self._table} SET {assignments} WHERE {self._pk} = ${len(data) + 1}"
            f" RETURNING {self._select_columns}",
            *data.values(),
            id,
        )
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
