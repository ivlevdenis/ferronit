"""Connection adapters for the raw data layer — one shape, three engines.

``rawdb``/``rawmodel`` talk to a connection through four operations — ``fetch``,
``fetchrow``, ``fetchval`` and ``execute`` — plus transaction control, and they
compile SQL through :mod:`ferronit.contrib.sql` for the connection's dialect.
An ``asyncpg`` connection already looks like that, so this module adapts MySQL
(``aiomysql``) and SQLite (``aiosqlite``) to the same shape: repositories then run
unchanged on all three engines.

Driver imports happen inside the factories: every driver is an extra
(``ferronit[postgres]``, ``ferronit[mysql]``, ``ferronit[sqlite]``), so the data
layer imports without any of them installed.

DSN shape (the scheme picks the driver)::

    postgresql://user:pass@host:5432/db      → asyncpg (see rawdb.create_pool)
    mysql://user:pass@host:3307/db           → aiomysql, dialect MYSQL
    sqlite:///absolute/path.db               → aiosqlite, dialect SQLITE
    sqlite://:memory:                        → aiosqlite in-memory

Both MySQL and SQLite connections expose ``dialect``; PostgreSQL connections do
not, and the repositories fall back to ``sql.POSTGRES`` for them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from ferronit.contrib import sql

__all__ = [
    "MISSING_EXTRA_HINT",
    "Connection",
    "MySQLConnection",
    "MySQLPool",
    "Pool",
    "Row",
    "SQLiteConnection",
    "SQLitePool",
    "open_pool",
]

MISSING_EXTRA_HINT = (
    "driver is not installed: pip install 'ferronit[{extra}]' "
    "(or poetry install --all-extras)"
)


class Row(tuple):
    """A positional row that also answers by column name.

    ``asyncpg`` returns rich ``Record`` objects (index by position *and* by name);
    the MySQL and SQLite cursors return plain tuples, while the data layer does
    both — ``dict(row)`` for the dict-returning repository and ``row[i]`` for model
    materialisation. ``Row`` restores the named access without copying the values.

    Args:
        names: Column names in select-list order.
        values: The row values.
    """

    _names: tuple[str, ...]

    def __new__(cls, names: Sequence[str], values: Sequence[Any]) -> Row:
        row = super().__new__(cls, values)
        row._names = tuple(names)
        return row

    def keys(self) -> list[str]:
        """Return the column names — this is what makes ``dict(row)`` work."""
        return list(self._names)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            return super().__getitem__(self._names.index(key))
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        """Return a column value by name, or ``default`` when it is absent.

        Args:
            key: Column name.
            default: Value returned when the column is not in the row.

        Returns:
            The column value or ``default``.
        """
        try:
            return self[key]
        except (IndexError, KeyError, ValueError):
            return default


@runtime_checkable
class Connection(Protocol):
    """What the data layer expects from a connection.

    An ``asyncpg`` connection satisfies this structurally; MySQL/SQLite go through
    the adapters below. ``dialect`` is a plain attribute (a class attribute on the
    adapters), so it costs nothing per call.
    """

    dialect: sql.Dialect

    async def fetch(self, sql_text: str, *params: Any) -> list[Any]:
        """Run a query and return all rows."""
        ...

    async def fetchrow(self, sql_text: str, *params: Any) -> Any | None:
        """Run a query and return the first row, or ``None``."""
        ...

    async def fetchval(self, sql_text: str, *params: Any) -> Any | None:
        """Run a query and return the first column of the first row."""
        ...

    async def execute(self, sql_text: str, *params: Any) -> Any:
        """Run a statement; on INSERT return the generated key when available."""
        ...

    async def executemany(self, sql_text: str, rows: Sequence[Sequence[Any]]) -> None:
        """Run one statement per row; the driver pipelines them into one batch.

        Args:
            sql_text: Statement with placeholders (no row count baked in).
            rows: Argument tuple per row, in placeholder order.
        """
        ...

    def is_in_transaction(self) -> bool:
        """Whether a transaction is currently open on this connection."""
        ...


@runtime_checkable
class Pool(Protocol):
    """What a unit of work expects from a pool.

    ``asyncpg.Pool`` satisfies it as is; the MySQL/SQLite adapters below wrap their
    native pools (an ``aiomysql`` pool, a single ``aiosqlite`` connection).
    """

    async def acquire(self) -> Any:
        """Take a connection from the pool."""
        ...

    async def release(self, connection: Any) -> None:
        """Give a connection back."""
        ...

    async def close(self) -> None:
        """Close the pool and its connections."""
        ...


def _require(module_name: str, extra: str) -> Any:
    """Import an optional driver, or explain which extra provides it.

    Args:
        module_name: Import name of the driver module.
        extra: Extra name in ``pyproject.toml``.

    Returns:
        The imported module.

    Raises:
        ModuleNotFoundError: If the driver is not installed.
    """
    try:
        return __import__(module_name)
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise ModuleNotFoundError(
            MISSING_EXTRA_HINT.format(extra=extra)
        ) from exc


def parse_dsn(dsn: str) -> dict[str, Any]:
    """Split a DSN into the keyword arguments its driver's ``connect`` expects.

    The scheme decides the shape: network engines (``mysql://``, ``postgresql://``)
    give ``host``/``port``/``user``/``password``/``db``; SQLite, being a file, gives
    ``path`` (``:memory:`` for an in-memory database).

    Args:
        dsn: Connection string, e.g. ``mysql://root:***@127.0.0.1:3307/db``
            or ``sqlite:///var/lib/app.db``.

    Returns:
        Keyword arguments for the driver: ``host``/``port``/``user``/``password``/``db``
        for network engines, ``path`` for SQLite.

    Raises:
        ValueError: If the DSN has no scheme, or the scheme is not a database scheme
            (a SQLite DSN parsed as MySQL would silently point at the wrong database).
    """
    parts = urlsplit(dsn)
    scheme = parts.scheme.lower()
    if not scheme:
        raise ValueError(f"DSN без схемы: {dsn!r}")
    if scheme.startswith("sqlite"):
        # Путь берём дословно после «://»: sqlite://:memory: → «:memory:»,
        # sqlite:///var/app.db → «/var/app.db». Так же читает open_pool.
        raw = dsn.split("://", 1)[1]
        return {"path": raw or ":memory:"}
    if scheme.startswith("mysql"):
        default_port = 3306
    elif scheme.startswith("postgres"):
        default_port = 5432
    else:
        raise ValueError(f"неизвестная схема DSN: {scheme!r} (mysql, postgresql, sqlite)")
    port = parts.port
    return {
        "host": parts.hostname or "127.0.0.1",
        "port": port if port is not None else default_port,
        "user": parts.username or "",
        "password": parts.password or "",
        "db": (parts.path or "/").lstrip("/"),
    }


# ── MySQL (aiomysql) ─────────────────────────────────────────────────────────


class MySQLConnection:
    """Adapter over an ``aiomysql`` connection — the shape the data layer wants.

    Values are bound by the driver (``%s``-style, see
    :class:`~ferronit.contrib.sql.MySQLDialect`); rows come back as
    :class:`Row`, so both ``dict(row)`` and positional indexing work.

    Args:
        connection: A live ``aiomysql`` connection.
    """

    dialect = sql.MYSQL

    __slots__ = ("_raw",)

    def __init__(self, connection: Any) -> None:
        self._raw = connection

    @property
    def raw(self) -> Any:
        """The underlying ``aiomysql`` connection."""
        return self._raw

    async def _run(self, sql_text: str, params: tuple[Any, ...]) -> Any:
        """Execute ``sql`` on a fresh cursor and return it (left open for reading)."""
        cursor = await self._raw.cursor()
        await cursor.execute(sql_text, params or None)
        return cursor

    @staticmethod
    def _rows(cursor: Any, values: Sequence[Sequence[Any]]) -> list[Row]:
        names = [column[0] for column in cursor.description or ()]
        return [Row(names, value) for value in values]

    async def fetch(self, sql_text: str, *params: Any) -> list[Row]:
        """Run a query and return all rows as :class:`Row` objects."""
        cursor = await self._run(sql_text, params)
        try:
            return self._rows(cursor, await cursor.fetchall())
        finally:
            await cursor.close()

    async def fetchrow(self, sql_text: str, *params: Any) -> Row | None:
        """Run a query and return the first row (closing the cursor after it)."""
        cursor = await self._run(sql_text, params)
        try:
            values = await cursor.fetchall()
            return self._rows(cursor, values)[0] if values else None
        finally:
            await cursor.close()

    async def fetchval(self, sql_text: str, *params: Any) -> Any | None:
        """Return the first column of the first row."""
        row = await self.fetchrow(sql_text, *params)
        return row[0] if row is not None else None

    async def execute(self, sql_text: str, *params: Any) -> Any:
        """Run a statement and return its ``lastrowid`` (generated key on INSERT)."""
        cursor = await self._run(sql_text, params)
        try:
            return cursor.lastrowid
        finally:
            await cursor.close()

    async def executemany(self, sql_text: str, rows: Sequence[Sequence[Any]]) -> None:
        """Run one statement per row on a single cursor.

        Args:
            sql_text: Statement with ``%s`` placeholders.
            rows: Argument tuple per row.
        """
        cursor = await self._raw.cursor()
        try:
            await cursor.executemany(sql_text, rows)
        finally:
            await cursor.close()

    def is_in_transaction(self) -> bool:
        """Whether the connection runs with autocommit off (explicit transactions)."""
        return not self._raw.get_autocommit()

    async def ping(self) -> None:
        """Ping the server (useful as a readiness probe)."""
        await self._raw.ping()


class MySQLPool:
    """Adapter over an ``aiomysql`` pool: hands out :class:`MySQLConnection`.

    Args:
        pool: A ready ``aiomysql`` pool.
    """

    __slots__ = ("_pool",)

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def acquire(self) -> MySQLConnection:
        """Take a connection from the pool and wrap it."""
        return MySQLConnection(await self._pool.acquire())

    async def release(self, connection: MySQLConnection) -> None:
        """Return a wrapped connection to the pool."""
        await self._pool.release(connection.raw)

    async def close(self) -> None:
        """Close the pool."""
        self._pool.close()
        await self._pool.wait_closed()


# ── SQLite (aiosqlite) ───────────────────────────────────────────────────────


class SQLiteConnection:
    """Adapter over an ``aiosqlite`` connection.

    SQLite has no server and no connection pool: one connection serialises the
    statements, which matches SQLite's single-writer model. Transactions are
    controlled explicitly by the unit of work (``BEGIN``/``COMMIT``), so the
    connection is opened with ``isolation_level=None``.

    Args:
        connection: A live ``aiosqlite`` connection.
    """

    dialect = sql.SQLITE

    __slots__ = ("_raw",)

    def __init__(self, connection: Any) -> None:
        self._raw = connection

    @property
    def raw(self) -> Any:
        """The underlying ``aiosqlite`` connection."""
        return self._raw

    async def fetch(self, sql_text: str, *params: Any) -> list[Row]:
        """Run a query and return all rows as :class:`Row` objects."""
        cursor = await self._raw.execute(sql_text, params)
        try:
            values = await cursor.fetchall()
            names = [column[0] for column in cursor.description or ()]
        finally:
            await cursor.close()
        return [Row(names, value) for value in values]

    async def fetchrow(self, sql_text: str, *params: Any) -> Row | None:
        """Run a query and return the first row, or ``None``."""
        rows = await self.fetch(sql_text, *params)
        return rows[0] if rows else None

    async def fetchval(self, sql_text: str, *params: Any) -> Any | None:
        """Return the first column of the first row."""
        row = await self.fetchrow(sql_text, *params)
        return row[0] if row is not None else None

    async def execute(self, sql_text: str, *params: Any) -> Any:
        """Run a statement and return its ``lastrowid`` (generated key on INSERT)."""
        cursor = await self._raw.execute(sql_text, params)
        try:
            return cursor.lastrowid
        finally:
            await cursor.close()

    async def executemany(self, sql_text: str, rows: Sequence[Sequence[Any]]) -> None:
        """Run one statement per row on a single cursor.

        Args:
            sql_text: Statement with ``?`` placeholders.
            rows: Argument tuple per row.
        """
        cursor = await self._raw.executemany(sql_text, rows)
        try:
            await cursor.close()
        except AttributeError:  # pragma: no cover - зависит от версии aiosqlite
            return

    def is_in_transaction(self) -> bool:
        """Whether an explicit transaction is open."""
        return bool(self._raw.in_transaction)

    async def close(self) -> None:
        """Close the underlying connection."""
        await self._raw.close()


class SQLitePool:
    """A pool of exactly one SQLite connection — the unit-of-work entry point.

    SQLite is a library, not a server: there is nothing to pool. The adapter exists
    so that ``RawUnitOfWork`` and ``Pool`` behave the same way as for the network
    engines; concurrent units of work share the connection and are therefore
    serialised (use one database file per writer, as SQLite requires).

    Args:
        connection: A live ``aiosqlite`` connection.
    """

    __slots__ = ("_connection",)

    def __init__(self, connection: Any) -> None:
        self._connection = SQLiteConnection(connection)

    @property
    def dialect(self) -> sql.Dialect:
        """The dialect of this pool (SQLite)."""
        return sql.SQLITE

    async def acquire(self) -> SQLiteConnection:
        """Return the single wrapped connection."""
        return self._connection

    async def release(self, connection: SQLiteConnection) -> None:
        """No-op: the connection stays open for the next unit of work."""

    async def close(self) -> None:
        """Close the connection."""
        await self._connection.close()


# ── Factory ──────────────────────────────────────────────────────────────────


async def open_pool(dsn: str, *, min_size: int = 1, max_size: int = 10) -> Any:
    """Open a native pool for an ``mysql://`` or ``sqlite://`` DSN.

    The driver is imported here, not at module import time, so that the optional
    extras stay optional.

    Args:
        dsn: Connection string; the scheme picks the driver.
        min_size: Minimum number of pooled connections (MySQL).
        max_size: Maximum number of pooled connections (MySQL).

    Returns:
        A pool adapter: :class:`MySQLPool` or :class:`SQLitePool`.

    Raises:
        ValueError: If the scheme is neither ``mysql`` nor ``sqlite``.
        ModuleNotFoundError: If the driver's extra is not installed.
    """
    scheme = urlsplit(dsn).scheme.lower()
    if scheme.startswith("mysql"):
        aiomysql = _require("aiomysql", "mysql")
        options = parse_dsn(dsn)
        pool = await aiomysql.create_pool(
            minsize=min_size,
            maxsize=max_size,
            autocommit=False,
            charset="utf8mb4",
            **options,
        )
        return MySQLPool(pool)
    if scheme.startswith("sqlite"):
        aiosqlite = _require("aiosqlite", "sqlite")
        connection = await aiosqlite.connect(parse_dsn(dsn)["path"], isolation_level=None)
        return SQLitePool(connection)
    raise ValueError(f"неизвестная схема DSN: {scheme!r} (нужно mysql или sqlite)")
