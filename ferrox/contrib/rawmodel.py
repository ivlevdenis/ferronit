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
from typing import Any

import asyncpg
import msgspec
from msgspec.structs import StructMeta  # type: ignore[attr-defined]
from msgspec.structs import fields as struct_fields

from ferrox.contrib.rawdb import Condition, _merge_filters
from ferrox.hexagonal import Adapter

__all__ = [
    "Column",
    "IdentityMap",
    "Model",
    "Query",
    "RawModelRepository",
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


class ModelMeta(StructMeta):
    """Metaclass: каждый подкласс ``Model`` — ``msgspec.Struct`` + колонки по порядку."""

    def __new__(mcs, name, bases, ns, **kw: Any):
        cls = super().__new__(mcs, name, bases, ns, **kw)
        if name != "Model":
            cls.__columns__ = tuple(f.name for f in struct_fields(cls))
            cls.__select_columns__ = ", ".join(cls.__columns__)
            cls.c = _Columns({name: Column(name) for name in cls.__columns__})
        return cls


class Model(msgspec.Struct, metaclass=ModelMeta):
    """Base class for a declarative row model.

    Subclass it and annotate the columns as fields, in the order ``SELECT *``
    returns them. The subclass is an immutable ``msgspec.Struct``: fast
    positional construction and C-level JSON serialisation.
    """

    __table__ = None

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
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __gt__(self, other: Any) -> Condition:
        return Condition(f"{self.name} > $1", [other])

    def __ge__(self, other: Any) -> Condition:
        return Condition(f"{self.name} >= $1", [other])

    def __lt__(self, other: Any) -> Condition:
        return Condition(f"{self.name} < $1", [other])

    def __le__(self, other: Any) -> Condition:
        return Condition(f"{self.name} <= $1", [other])

    def __eq__(self, other: Any) -> Condition:  # type: ignore[override]
        if other is None:
            return Condition(f"{self.name} IS NULL", [])
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

    __slots__ = ("_limit", "_model", "_offset", "_order", "_select", "_table", "_where")

    def __init__(self, model: type[Model], table: str | None = None) -> None:
        self._model = model
        self._table = table or _resolve_table(model)
        # Предкомпиляция статики: колонки известны на уровне класса.
        self._select = f"SELECT {model.__select_columns__} FROM {self._table}"
        self._where: list[Condition] = []
        self._order: list[str] = []
        self._limit: int | None = None
        self._offset: int | None = None

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
        return sql, params


def select(model: type[Model], table: str | None = None) -> Query:
    """Start a ``SELECT`` over ``model``."""
    return Query(model, table)


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
        "_insert_columns",
        "_insert_sql",
        "_model",
        "_pk",
        "_select_columns",
        "_select_pk_sql",
        "_table",
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
        self._table = table or _resolve_table(model_cls)
        self._identity_map = identity_map
        self._columns = model_cls.__columns__
        self._select_columns = model_cls.__select_columns__
        self._insert_columns = tuple(c for c in self._columns if c != pk)
        columns = ", ".join(self._insert_columns)
        placeholders = ", ".join(f"${i}" for i in range(1, len(self._insert_columns) + 1))
        self._insert_sql = (
            f"INSERT INTO {self._table} ({columns}) VALUES ({placeholders})"
            f" RETURNING {self._select_columns}"
        )
        self._select_pk_sql = (
            f"SELECT {self._select_columns} FROM {self._table} WHERE {self._pk} = $1"
        )
        self._delete_sql = f"DELETE FROM {self._table} WHERE {self._pk} = $1"

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
        where, params = _merge_filters(filters)
        clause = f" WHERE {where}" if where else ""
        sql = (
            f"SELECT {self._select_columns} FROM {self._table}{clause}"
            f" LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
        )
        rows = await self._conn.fetch(sql, *params, limit, offset)
        # Identity-map наполняется только из get(): на list() карта почти никогда
        # не пригождается, а класть каждую строку в неё — лишние ~50 µs на 1000 строк.
        return [self._model(*row) for row in rows]

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
        return [self._model(*row) for row in rows]

    async def fetch_one(self, query: Query) -> M | None:
        """Fetch the first matching row, or ``None``.

        Adds ``LIMIT 1`` when the query has no limit yet, so the database does
        not materialise the whole result set.
        """
        if query._limit is None:
            query = query.limit(1)
        sql, params = query.compile()
        row = await self._conn.fetchrow(sql, *params)
        return self._model(*row) if row is not None else None

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
