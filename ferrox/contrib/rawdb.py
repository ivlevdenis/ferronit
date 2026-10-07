"""PostgreSQL adapter on a raw driver — thin asyncpg repository and Unit of Work.

Why this module exists: it is a measured experiment. The SQLAlchemy layer costs
roughly 0.43–0.55 ms per request inside this framework (see ``bench/README.md``),
so this adapter keeps the repository surface while talking to ``asyncpg`` directly.

What it deliberately does **not** have: any query builder, ORM mapping, unit of
work identity map, relationship loading or migration support. Filters are
explicit SQL fragments with bound parameters, and rows come back as ``dict``s.
That is the trade-off: less comfort, and no protection from N+1 style mistakes,
in exchange for roughly three times the throughput of the SQLAlchemy Core path
on the same queries.

Lifecycle rules worth knowing:

* an ``asyncpg`` pool is bound to the event loop it was created in — build it
  where the server runs (``create_raw_pool``), not at import time;
* one ``RawUnitOfWork`` serves one request; sharing a single instance between
  concurrent tasks corrupts the connection state.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import asyncpg

from ferrox.hexagonal import Adapter, UnitOfWork

__all__ = [
    "Condition",
    "RawRepository",
    "RawUnitOfWork",
    "create_raw_pool",
]


class Condition:
    """A SQL fragment with bound parameters, used as a repository filter.

    The fragment is written by the caller — this adapter has no query builder.
    Placeholders are PostgreSQL-style (``$1``, ``$2``, ...) and are renumbered
    automatically when several conditions are combined.

    Example:
        Condition("age > $1", [18])
    """

    __slots__ = ("params", "sql")

    def __init__(self, sql: str, params: Sequence[Any] = ()) -> None:
        self.sql = sql
        self.params = list(params)

    def __and__(self, other) -> Condition:
        """Combine two conditions with ``AND`` (``a & b``)."""
        if not isinstance(other, Condition):
            return NotImplemented
        right = _renumber_placeholders(other.sql, len(other.params), len(self.params))
        return Condition(f"({self.sql}) AND ({right})", [*self.params, *other.params])

    def __or__(self, other) -> Condition:
        """Combine two conditions with ``OR`` (``a | b``)."""
        if not isinstance(other, Condition):
            return NotImplemented
        right = _renumber_placeholders(other.sql, len(other.params), len(self.params))
        return Condition(f"({self.sql}) OR ({right})", [*self.params, *other.params])

    def or_(self, other: Condition) -> Condition:
        """Fluent ``OR`` — ``a.or_(b)`` (читается лучше, чем ``(a) | (b)``)."""
        return self | other

    def and_(self, other: Condition) -> Condition:
        """Fluent ``AND`` — ``a.and_(b)``."""
        return self & other


def _renumber_placeholders(sql: str, count: int, offset: int) -> str:
    """Renumber ``$1..$count`` by ``offset`` (descending — ``$1`` не затирает ``$10``)."""
    for index in range(count, 0, -1):
        sql = re.sub(rf"\${index}(?!\d)", f"${index + offset}", sql)
    return sql


def _merge_filters(filters: Sequence[Condition]) -> tuple[str, list[Any]]:
    """Combine conditions into one ``WHERE`` clause with continuous placeholders.

    Args:
        filters: Conditions to join with ``AND``.

    Returns:
        A tuple of the clause (empty string when there is nothing to filter by)
        and the flattened parameter list.

    Example:
        ``(_merge_filters([Condition("a > $1", [1]), Condition("b < $1", [9])]))``
        returns ``("(a > $1) AND (b < $2)", [1, 9])``.
    """
    clauses: list[str] = []
    params: list[Any] = []
    for condition in filters:
        clauses.append(
            f"({_renumber_placeholders(condition.sql, len(condition.params), len(params))})"
        )
        params.extend(condition.params)
    return " AND ".join(clauses), params


class RawRepository(Adapter):
    """Repository over one table, backed by a single asyncpg connection.

    Usage:
        repo = RawRepository(connection, "users")
        await repo.save({"name": "A"})          # → {"name": "A", "id": 1}
        await repo.get(1)                       # → {"id": 1, "name": "A"} | None
        await repo.list(Condition("age > $1", [18]), limit=50)
        await repo.update(1, {"name": "B"})
        await repo.delete(1)
    """

    __slots__ = ("_conn", "_pk", "_table")

    def __init__(self, connection: asyncpg.Connection, table: str, pk: str = "id") -> None:
        self._conn = connection
        self._table = table
        self._pk = pk

    async def get(self, id: Any) -> dict[str, Any] | None:
        """Fetch a single row by primary key.

        Args:
            id: Primary key value.

        Returns:
            The row as a dict, or ``None`` when nothing matches.
        """
        row = await self._conn.fetchrow(
            f"SELECT * FROM {self._table} WHERE {self._pk} = $1", id
        )
        return dict(row) if row is not None else None

    async def list(
        self, *filters: Condition, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """List rows, optionally filtered and paginated.

        Args:
            *filters: Conditions joined with ``AND``.
            limit: Maximum number of rows to return.
            offset: Number of rows to skip.

        Returns:
            A list of rows, each as a dict (possibly empty).
        """
        where, params = _merge_filters(filters)
        clause = f" WHERE {where}" if where else ""
        sql = (
            f"SELECT * FROM {self._table}{clause}"
            f" LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
        )
        rows = await self._conn.fetch(sql, *params, limit, offset)
        return [dict(row) for row in rows]

    async def save(self, data: dict[str, Any], *, returning: bool = False) -> dict[str, Any]:
        """INSERT a row and return it as a dict.

        The primary key is always read back with ``RETURNING``, so generated
        keys (``serial``, ``identity``) come from the server rather than from a
        driver-side guess.

        Args:
            data: Column name → value mapping to insert.
            returning: Return the full server-side row instead of the input
                columns plus the generated primary key.

        Returns:
            The inserted row as a dict.
        """
        columns = ", ".join(data)
        placeholders = ", ".join(f"${index}" for index in range(1, len(data) + 1))
        if returning:
            row = await self._conn.fetchrow(
                f"INSERT INTO {self._table} ({columns}) VALUES ({placeholders}) RETURNING *",
                *data.values(),
            )
            return dict(row) if row is not None else dict(data)
        if self._pk in data:
            await self._conn.execute(
                f"INSERT INTO {self._table} ({columns}) VALUES ({placeholders})", *data.values()
            )
            return dict(data)
        value = await self._conn.fetchval(
            f"INSERT INTO {self._table} ({columns}) VALUES ({placeholders})"
            f" RETURNING {self._pk}",
            *data.values(),
        )
        return {**data, self._pk: value}

    async def update(self, id: Any, data: dict[str, Any]) -> dict[str, Any]:
        """UPDATE a row by primary key and return the merged dict.

        Args:
            id: Primary key value of the row to update.
            data: Column name → new value mapping.

        Returns:
            ``data`` merged with the primary key (no read-back is performed).
        """
        if not data:
            return {self._pk: id}
        assignments = ", ".join(f"{column} = ${index}" for index, column in enumerate(data, 1))
        await self._conn.execute(
            f"UPDATE {self._table} SET {assignments} WHERE {self._pk} = ${len(data) + 1}",
            *data.values(),
            id,
        )
        return {**data, self._pk: id}

    async def delete(self, id: Any) -> None:
        """Delete a row by primary key.

        Args:
            id: Primary key value of the row to delete.
        """
        await self._conn.execute(f"DELETE FROM {self._table} WHERE {self._pk} = $1", id)


class RawUnitOfWork(UnitOfWork):
    """Unit of Work over an asyncpg pool: one connection, one transaction.

    Create one instance per request — the object holds the connection state.
    By default it opens an explicit transaction (``BEGIN``/``COMMIT``) so that
    writes commit atomically and roll back on error. For read-only units pass
    ``readonly=True`` — the transaction is skipped (one round-trip saved), and
    ``SELECT``/``get``/``list`` run in autocommit.

    Usage:
        uow = RawUnitOfWork(pool)
        async with uow:
            repo = uow["users"]                 # table name → RawRepository
            await repo.save({"name": "A"})
            # committed on clean exit, rolled back on exception
    """

    __slots__ = ("_conn", "_identity_map", "_pk", "_pool", "_readonly")

    def __init__(
        self, pool: asyncpg.Pool, pk: str = "id", *, readonly: bool = False
    ) -> None:
        self._pool = pool
        self._pk = pk
        self._readonly = readonly
        self._conn: asyncpg.Connection | None = None
        self._identity_map: Any | None = None

    @property
    def connection(self) -> asyncpg.Connection:
        """Connection acquired for the current context.

        Returns:
            The acquired ``asyncpg`` connection.

        Raises:
            RuntimeError: If accessed outside an ``async with`` block.
        """
        if self._conn is None:
            raise RuntimeError("No active connection")
        return self._conn

    def __getitem__(self, table: str) -> RawRepository:
        """Build a repository for a table name."""
        return RawRepository(self.connection, table, self._pk)

    def model(
        self, model_cls: type, *, table: str | None = None, pk: str = "id"
    ) -> Any:
        """Build a model-returning repository sharing this unit's connection.

        The repository returns ``rawmodel.Model`` instances instead of dicts and
        shares one ``IdentityMap`` for the whole unit of work, so repeated point
        reads (``get``) of the same row hand back the same instance.

        Args:
            model_cls: A ``ferrox.contrib.rawmodel.Model`` subclass.
            table: Table name; defaults to the model's resolved name.
            pk: Primary-key column name.

        Returns:
            A ``RawModelRepository`` bound to this unit's connection.
        """
        from ferrox.contrib.rawmodel import IdentityMap, RawModelRepository

        if self._identity_map is None:
            self._identity_map = IdentityMap()
        return RawModelRepository(self.connection, model_cls, table, pk, self._identity_map)

    async def commit(self) -> None:
        """Commit the current transaction, if one is open."""
        if self._conn is not None and self._conn.is_in_transaction():
            await self._conn.execute("COMMIT")

    async def rollback(self) -> None:
        """Roll back the current transaction, if one is open."""
        if self._conn is not None and self._conn.is_in_transaction():
            await self._conn.execute("ROLLBACK")

    async def __aenter__(self) -> RawUnitOfWork:
        conn = await self._pool.acquire()
        try:
            # без явного BEGIN asyncpg коммитит каждый оператор сам,
            # и Unit of Work перестаёт быть единицей работы (откат не работает).
            # В read-only режиме транзакция не нужна — читаем в autocommit.
            if not self._readonly:
                await conn.execute("BEGIN")
        except BaseException:
            await self._pool.release(conn)
            raise
        self._conn = conn
        return self

    async def __aexit__(self, *args: object) -> None:
        conn = self._conn
        if conn is None:
            return
        try:
            if args[0] is None:
                await self.commit()
            else:
                await self.rollback()
        finally:
            self._conn = None
            await self._pool.release(conn)


async def create_raw_pool(
    dsn: str, *, min_size: int = 1, max_size: int = 10
) -> asyncpg.Pool:
    """Create an asyncpg pool for use with ``RawUnitOfWork``.

    Call this inside the event loop the server runs in: ``asyncpg`` binds the
    pool to the loop it was created in.

    Args:
        dsn: libpq-style connection string, e.g. ``postgresql://user@host/db``.
        min_size: Minimum number of pooled connections.
        max_size: Maximum number of pooled connections.

    Returns:
        A ready-to-use ``asyncpg`` pool with ``dict``-style row access disabled
        (rows are converted to dicts by the repository).
    """
    return await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
