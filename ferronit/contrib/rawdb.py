"""PostgreSQL adapter on a raw driver — thin asyncpg repository and Unit of Work.

This is Ferronit's single data layer: a thin repository/UnitOfWork over asyncpg,
with rows as ``dict``s. It deliberately has no ORM mapping, relationship loading
or migration support — filters are explicit SQL fragments with bound parameters,
which keeps the hot path fast and predictable.

Lifecycle rules worth knowing:

* an ``asyncpg`` pool is bound to the event loop it was created in — build it
  where the server runs (``create_raw_pool``), not at import time;
* one ``RawUnitOfWork`` serves one request; sharing a single instance between
  concurrent tasks corrupts the connection state.

The PostgreSQL driver lives in the ``postgres`` extra and is imported lazily, so
this module imports fine in projects that only use MySQL or SQLite.
"""

from __future__ import annotations

import builtins
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from ferronit.contrib import sql
from ferronit.contrib.drivers import MISSING_EXTRA_HINT
from ferronit.hexagonal import Adapter, UnitOfWork

if TYPE_CHECKING:  # драйвер ставится extra "postgres", в рантайме его может не быть
    import asyncpg

__all__ = [
    "Condition",
    "Pool",
    "RawRepository",
    "RawUnitOfWork",
    "create_pool",
    "create_raw_pool",
]


def _require_asyncpg() -> Any:
    """Import ``asyncpg``, or explain which extra provides it.

    The driver is imported here, not at module import time, so that the data
    layer stays importable without the ``postgres`` extra.

    Returns:
        The ``asyncpg`` module.

    Raises:
        ModuleNotFoundError: If the ``postgres`` extra is not installed.
    """
    try:
        import asyncpg
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise ModuleNotFoundError(MISSING_EXTRA_HINT.format(extra="postgres")) from exc
    return asyncpg


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

    def __invert__(self) -> Condition:
        """Negate a condition — ``~a`` → ``NOT (a)``."""
        return Condition(f"NOT ({self.sql})", list(self.params))

    def __or__(self, other) -> Condition:
        """Combine two conditions with ``OR`` (``a | b``)."""
        if not isinstance(other, Condition):
            return NotImplemented
        right = _renumber_placeholders(other.sql, len(other.params), len(self.params))
        return Condition(f"({self.sql}) OR ({right})", [*self.params, *other.params])


def _renumber_placeholders(sql_text: str, count: int, offset: int) -> str:
    """Renumber ``$1..$count`` by ``offset`` (descending — ``$1`` не затирает ``$10``).

    Thin wrapper over :func:`sql.renumber_placeholders`; kept with the
    historical signature because ``Condition`` is a public raw-SQL escape hatch.
    """
    return sql.renumber_placeholders(sql_text, offset)


def _merge_filters(
    filters: Sequence[Any], dialect: sql.Dialect = sql.POSTGRES
) -> tuple[str, list[Any]]:
    """Combine filters into one ``WHERE`` clause with continuous placeholders.

    Accepts raw :class:`Condition` fragments (their ``$N`` placeholders are
    renumbered) and compiled expression nodes from :mod:`ferronit.contrib.sql`
    (``User.c.age > 18``); every item is wrapped in parentheses and joined with
    ``AND``.

    Args:
        filters: Conditions or expression nodes to join with ``AND``.
        dialect: Target dialect for placeholders (PostgreSQL by default).

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
        sql_text, values = sql.compile_expr(condition, dialect, offset=len(params))
        clauses.append(f"({sql_text})")
        params.extend(values)
    return " AND ".join(clauses), params


class RawRepository(Adapter):
    """Repository over one table, backed by a single connection.

    The connection decides the dialect: an ``asyncpg`` connection renders
    PostgreSQL (``$1`` placeholders, ``RETURNING``), while the MySQL/SQLite
    adapters from :mod:`ferronit.contrib.drivers` render their own placeholder
    style and upsert syntax. On engines without ``RETURNING`` the generated key
    comes from the driver (``lastrowid``) and the row is read back separately.

    Usage:
        repo = RawRepository(connection, "users")
        await repo.save({"name": "A"})          # → {"name": "A", "id": 1}
        await repo.get(1)                       # → {"id": 1, "name": "A"} | None
        await repo.list(Condition("age > $1", [18]), limit=50)
        await repo.update(1, {"name": "B"})
        await repo.delete(1)
    """

    __slots__ = ("_conn", "_dialect", "_pk", "_table")

    def __init__(self, connection: Any, table: str, pk: str = "id") -> None:
        self._conn = connection
        self._table = table
        self._pk = pk
        # Диалект берём у соединения; у asyncpg атрибута нет — значит PostgreSQL.
        self._dialect = getattr(connection, "dialect", sql.POSTGRES)

    async def get(self, id: Any) -> dict[str, Any] | None:
        """Fetch a single row by primary key.

        Args:
            id: Primary key value.

        Returns:
            The row as a dict, or ``None`` when nothing matches.
        """
        sql_text, _ = sql.compile_statement(
            sql.Select(self._table, ("*",), where=[sql.Raw(f"{self._pk} = $1")]),
            self._dialect,
        )
        row = await self._conn.fetchrow(sql_text, id)
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
        sql_text, params = sql.compile_statement(
            sql.Select(
                self._table,
                ("*",),
                where=[sql.Paren(f) for f in filters],
                limit=limit,
                offset=offset,
            ),
            self._dialect,
        )
        rows = await self._conn.fetch(sql_text, *params)
        return [dict(row) for row in rows]

    def _returning_columns(self, returning: bool | Sequence[str]) -> Sequence[str]:
        """Normalise the ``returning`` argument into a column list.

        Args:
            returning: ``False`` — no clause at all; ``True`` — the whole row
                (``*``); a sequence — exactly those columns.

        Returns:
            Columns of the ``RETURNING`` clause; empty means none.
        """
        if returning is True:
            return ("*",)
        if returning is False:
            return ()
        return tuple(returning)

    @staticmethod
    def _project(row: Any, wanted: Sequence[str]) -> dict[str, Any]:
        """Return the row as a dict, narrowed to the requested columns."""
        if tuple(wanted) == ("*",):
            return dict(row)
        return {column: row[column] for column in wanted}

    async def save(
        self, data: dict[str, Any], *, returning: bool | Sequence[str] = False
    ) -> dict[str, Any]:
        """INSERT a row and return it as a dict.

        Where the engine supports ``RETURNING`` (PostgreSQL, SQLite) the inserted
        row comes back in one round-trip; MySQL has no ``RETURNING``, so the
        generated key is taken from the driver and the row is read back with a
        second query (only when ``returning`` asks for it).

        Args:
            data: Column name → value mapping to insert.
            returning: What to read back instead of the input columns plus the
                generated primary key — ``True`` for the whole server row, or an
                explicit column list.

        Returns:
            The inserted row as a dict; with a column list only those columns.
        """
        columns = list(data)
        wanted = self._returning_columns(returning)
        if not self._dialect.supports_returning:
            key = await self._conn.execute(
                sql.compile_statement(sql.Insert(self._table, columns), self._dialect)[0],
                *data.values(),
            )
            if wanted:
                row = await self.get(self._pk if self._pk in data else key)
                if row is None:
                    return {**data, self._pk: key}
                return self._project(row, wanted)
            return {**data, self._pk: key} if self._pk not in data else dict(data)
        if wanted:
            sql_text, _ = sql.compile_statement(
                sql.Insert(self._table, columns, returning=wanted), self._dialect
            )
            row = await self._conn.fetchrow(sql_text, *data.values())
            if row is None:
                return dict(data)
            return self._project(row, wanted)
        if self._pk in data:
            sql_text, _ = sql.compile_statement(
                sql.Insert(self._table, columns), self._dialect
            )
            await self._conn.execute(sql_text, *data.values())
            return dict(data)
        sql_text, _ = sql.compile_statement(
            sql.Insert(self._table, columns, returning=(self._pk,)), self._dialect
        )
        value = await self._conn.fetchval(sql_text, *data.values())
        return {**data, self._pk: value}

    async def save_many(
        self,
        rows: Sequence[dict[str, Any]],
        *,
        returning: bool | Sequence[str] = False,
        chunk_size: int | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """INSERT many rows in bulk — one multi-row statement per chunk.

        A chunk travels as a single statement, so the batch costs one round-trip
        per chunk instead of one per row, and the chunk size keeps that statement
        inside the engine's placeholder limit (:attr:`sql.Dialect.max_parameters`)
        — that limit, not the row count, is what breaks a naive bulk insert.

        On engines with ``RETURNING`` (PostgreSQL, SQLite) the server rows come
        back with the generated keys. MySQL has no ``RETURNING``: the chunk still
        goes in as one statement, but per-row generated keys are only available
        one row at a time — so ``returning`` there falls back to the single-row
        path (N round-trips), and without ``returning`` the input rows come back
        as they were.

        Args:
            rows: Mappings to insert; all must have the same column names.
            returning: What to read back per row — ``True`` for the whole server
                row, a column list for exactly those columns.
            chunk_size: Rows per statement; by default the placeholder limit
                divided by the number of columns.

        Returns:
            The inserted rows as dicts, in input order.

        Raises:
            ValueError: If the rows do not carry the same columns.
        """
        if not rows:
            return []
        columns = list(rows[0])
        if any(list(row) != columns for row in rows):
            raise ValueError("save_many needs rows with the same columns")
        wanted = self._returning_columns(returning)
        can_return = self._dialect.supports_returning
        if wanted and not can_return:
            return [await self.save(dict(row), returning=returning) for row in rows]
        limit: int = (
            chunk_size
            if chunk_size is not None
            else max(1, self._dialect.max_parameters // max(1, len(columns)))
        )
        keys_wanted = (self._pk,) if can_return and self._pk not in columns else ()
        saved: list[dict[str, Any]] = []
        for start in range(0, len(rows), limit):
            chunk = rows[start : start + limit]
            values: list[Any] = []
            for row in chunk:
                values.extend(row.values())
            if wanted or keys_wanted:
                sql_text, _ = sql.compile_statement(
                    sql.Insert(
                        self._table,
                        columns,
                        rows=len(chunk),
                        returning=wanted or keys_wanted,
                    ),
                    self._dialect,
                )
                fetched = await self._conn.fetch(sql_text, *values)
                if wanted:
                    saved.extend(self._project(row, wanted) for row in fetched)
                else:
                    saved.extend(
                        {**row, self._pk: key[self._pk]}
                        for row, key in zip(chunk, fetched, strict=True)
                    )
                continue
            sql_text, _ = sql.compile_statement(
                sql.Insert(self._table, columns, rows=len(chunk)), self._dialect
            )
            await self._conn.execute(sql_text, *values)
            saved.extend(dict(row) for row in chunk)
        return saved

    async def update(
        self,
        id: Any,
        data: dict[str, Any],
        *,
        returning: bool | Sequence[str] = False,
    ) -> dict[str, Any] | None:
        """UPDATE a row by primary key and return the merged dict.

        Args:
            id: Primary key value of the row to update.
            data: Column name → new value mapping.
            returning: What to read back instead of the merged input — ``True``
                for the whole server row, or an explicit column list. On an
                engine without ``RETURNING`` (MySQL) the row is read back with a
                second query.

        Returns:
            ``data`` merged with the primary key (no read-back is performed), or
            the server row when ``returning`` is set — and then ``None`` means no
            row carried that key.
        """
        if not data:
            return {self._pk: id}
        wanted = self._returning_columns(returning)
        if not wanted:
            sql_text, _ = sql.compile_statement(
                sql.Update(self._table, list(data), self._pk), self._dialect
            )
            await self._conn.execute(sql_text, *data.values(), id)
            return {**data, self._pk: id}
        # RETURNING подставляем только там, где движок его понимает: MySQL
        # дочитывает строку вторым запросом.
        sql_text, _ = sql.compile_statement(
            sql.Update(
                self._table,
                list(data),
                self._pk,
                returning=wanted if self._dialect.supports_returning else (),
            ),
            self._dialect,
        )
        if self._dialect.supports_returning:
            row = await self._conn.fetchrow(sql_text, *data.values(), id)
        else:
            await self._conn.execute(sql_text, *data.values(), id)
            row = await self.get(id)
        return None if row is None else self._project(row, wanted)

    async def delete(
        self, id: Any, *, returning: bool | Sequence[str] = False
    ) -> dict[str, Any] | None:
        """Delete a row by primary key.

        Args:
            id: Primary key value of the row to delete.
            returning: What to report about the deleted row — ``True`` for the
                whole row, or an explicit column list. Without ``RETURNING`` on
                the engine (MySQL) the row is read *before* the delete, so the
                answer is the row as it was.

        Returns:
            ``None`` for a plain delete (the signature it always had); with
            ``returning`` — the deleted row (or the requested columns), and
            ``None`` when there was no such row.
        """
        wanted = self._returning_columns(returning)
        if not wanted:
            sql_text, _ = sql.compile_statement(
                sql.Delete(self._table, self._pk), self._dialect
            )
            await self._conn.execute(sql_text, id)
            return None
        if self._dialect.supports_returning:
            sql_text, _ = sql.compile_statement(
                sql.Delete(self._table, self._pk, returning=wanted), self._dialect
            )
            row = await self._conn.fetchrow(sql_text, id)
        else:
            row = await self.get(id)
            sql_text, _ = sql.compile_statement(
                sql.Delete(self._table, self._pk), self._dialect
            )
            await self._conn.execute(sql_text, id)
        return None if row is None else self._project(row, wanted)


class RawUnitOfWork(UnitOfWork):
    """Unit of Work over a pool: one connection, one transaction.

    Works with any driver's pool (``asyncpg``, ``aiomysql``, the SQLite adapter): it
    only needs ``acquire``/``release`` from the pool and ``execute``/
    ``is_in_transaction`` from the connection, so ``BEGIN``/``COMMIT``/``ROLLBACK``
    are issued the same way everywhere.

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
        self, pool: Any, pk: str = "id", *, readonly: bool = False
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
            model_cls: A ``ferronit.contrib.rawmodel.Model`` subclass.
            table: Table name; defaults to the model's resolved name.
            pk: Primary-key column name.

        Returns:
            A ``RawModelRepository`` bound to this unit's connection.
        """
        from ferronit.contrib.rawmodel import IdentityMap, RawModelRepository

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


class Pool:
    """Typed wrapper around a native pool — the unit-of-work entry point.

    The pool can be ``asyncpg`` (PostgreSQL), an ``aiomysql`` pool or the single
    SQLite connection adapter (see :mod:`ferronit.contrib.drivers`): the wrapper
    only needs ``acquire``/``release``/``close``, which all three provide. The
    dialect travels with the connection, not with the pool.

    Distinct pools are told apart by annotation: ``Annotated[Pool, "users"]``
    vs ``Annotated[Pool, "orders"]``. The wrapper keeps ``RawUnitOfWork``
    construction on the pool, so repositories depend on a specific pool type,
    not on the raw driver.

    Args:
        pool: A ready pool (from :func:`create_pool` or the driver directly).
    """

    __slots__ = ("_pool",)

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def uow(self, pk: str = "id", *, readonly: bool = False) -> RawUnitOfWork:
        """Return a :class:`RawUnitOfWork` over a connection from this pool.

        Args:
            pk: Primary key column used by the repository.
            readonly: Skip ``BEGIN`` (read-only operations).

        Returns:
            A unit of work; use it as ``async with pool.uow() as uow:``.
        """
        return RawUnitOfWork(self._pool, pk, readonly=readonly)

    async def close(self) -> None:
        """Close the underlying pool."""
        await self._pool.close()


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

    Raises:
        ModuleNotFoundError: If the ``postgres`` extra is not installed.
    """
    asyncpg = _require_asyncpg()
    return await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)


async def create_pool(
    dsn: str, *, min_size: int = 1, max_size: int = 10
) -> Pool:
    """Create a :class:`Pool` over the engine named by the DSN scheme.

    ``postgresql://`` → ``asyncpg`` (extra ``postgres``), ``mysql://`` →
    ``aiomysql`` (extra ``mysql``), ``sqlite://`` → ``aiosqlite`` (extra
    ``sqlite``; SQLite is a library, so the "pool" is one connection). The returned
    object is the same :class:`Pool` for every engine, so ``uow()``, the
    repositories and the examples do not change.

    Call this inside the event loop the server runs in (typically in
    ``@app.lifespan`` setup): the drivers bind their pools to that loop.

    Args:
        dsn: Connection string; the scheme picks the driver, e.g.
            ``postgresql://user@host/db``, ``mysql://user:pass@host:3307/db``,
            ``sqlite:///tmp/app.db`` or ``sqlite://:memory:``.
        min_size: Minimum number of pooled connections (network engines).
        max_size: Maximum number of pooled connections (network engines).

    Returns:
        A :class:`Pool` ready to hand out units of work.

    Raises:
        ValueError: If the DSN scheme is not one of the three engines.
        ModuleNotFoundError: If the driver's extra is not installed.
    """
    scheme = dsn.split("://", 1)[0].lower() if "://" in dsn else ""
    if scheme.startswith("postgres"):
        asyncpg = _require_asyncpg()
        return Pool(await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size))
    if scheme.startswith(("mysql", "sqlite")):
        from ferronit.contrib import drivers

        return Pool(await drivers.open_pool(dsn, min_size=min_size, max_size=max_size))
    raise ValueError(
        f"неизвестная схема DSN: {scheme!r} (поддерживаются postgresql, mysql, sqlite)"
    )
