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

import asyncio
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
    native pools (an ``aiomysql`` pool, an ``aiosqlite`` writer plus readers).
    Pools that can pick a dedicated read-only connection set
    ``supports_readonly_acquire = True`` and accept ``readonly`` — the unit of
    work passes it only then, so a bare ``asyncpg.Pool`` keeps working.
    """

    async def acquire(self, *, readonly: bool = False) -> Any:
        """Take a connection from the pool.

        Args:
            readonly: Preferred connection flavour for a read-only unit of work.
        """
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
    except ImportError as exc:  # pragma: no cover - environment-dependent
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
        raise ValueError(f"DSN has no scheme: {dsn!r}")
    if scheme.startswith("sqlite"):
        # The path is taken verbatim after «://»: sqlite://:memory: → «:memory:»,
        # sqlite:///var/app.db → «/var/app.db». open_pool reads it the same way.
        raw = dsn.split("://", 1)[1]
        return {"path": raw or ":memory:"}
    if scheme.startswith("mysql"):
        default_port = 3306
    elif scheme.startswith("postgres"):
        default_port = 5432
    else:
        raise ValueError(f"Unknown DSN scheme: {scheme!r} (mysql, postgresql, sqlite)")
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

    #: ``acquire`` understands the ``readonly`` keyword (see ``RawUnitOfWork``).
    supports_readonly_acquire = True

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def acquire(self, *, readonly: bool = False) -> MySQLConnection:
        """Take a connection from the pool and wrap it.

        Args:
            readonly: Accepted for the shared pool protocol; MySQL has no
                read-only connection flavour, so it is ignored.
        """
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

    Statements on one connection are serialised by SQLite itself, which is why
    the pool hands the writer out one unit of work at a time and gives read-only
    units their own connections (see :class:`SQLitePool`). Transactions are
    controlled explicitly by the unit of work (``BEGIN IMMEDIATE``/``COMMIT``),
    so the connection is opened with ``isolation_level=None``.

    Args:
        connection: A live ``aiosqlite`` connection.
    """

    dialect = sql.SQLITE

    #: SQLite takes the write lock up front: a deferred ``BEGIN`` that later
    #: upgrades fails with ``database is locked`` *after* the reads, and that
    #: transaction cannot be retried without losing work.
    begin_statement = "BEGIN IMMEDIATE"

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
        except AttributeError:  # pragma: no cover - depends on the aiosqlite version
            return

    def is_in_transaction(self) -> bool:
        """Whether an explicit transaction is open."""
        return bool(self._raw.in_transaction)

    async def close(self) -> None:
        """Close the underlying connection."""
        await self._raw.close()


class SQLitePool:
    """SQLite access as one writer plus N readers over WAL. Unit-of-work entry point.

    SQLite allows many readers and exactly one writer, so a symmetric pool would
    only turn the second writer into a ``database is locked`` error. The pool
    therefore has two sides:

    * **writer** — the single connection handed out to non-readonly units, one at
      a time (``asyncio.Lock``). Its transactions open with ``BEGIN IMMEDIATE``:
      a deferred ``BEGIN`` that upgrades later fails *after* the reads, and such a
      transaction cannot be retried without repeating the work.
    * **readers** — ``readers`` extra connections to the same file, opened in WAL
      mode with ``PRAGMA query_only = ON``. Read-only units borrow them
      concurrently, so the CQRS read side runs in parallel with writes.

    Readers see committed data only: a read-only unit never opens a transaction,
    and it is not attached to the writer's one. A misused read-only unit that
    tries to write fails loudly (``attempt to write a readonly database``)
    instead of quietly joining the writer's transaction.

    Limitations, both inherent to SQLite: an in-memory database (``:memory:``)
    is private per connection, so it gets the writer only and no readers; and the
    writer lock lives in-process, so several workers over one file still have to
    wait for each other through ``busy_timeout`` — keep one worker per file.

    Args:
        connection: A live ``aiosqlite`` connection used as the writer.
        readers: Extra read-only connections opened on the same database file.
            ``0`` disables parallel reads; in-memory databases always get ``0``.
        busy_timeout: Seconds a writer waits for another *process* holding the
            file lock before failing (``PRAGMA busy_timeout``).
        foreign_keys: Enable ``PRAGMA foreign_keys`` on every connection —
            SQLite does not enforce foreign keys otherwise.
    """

    __slots__ = (
        "_busy_timeout",
        "_connection",
        "_foreign_keys",
        "_path",
        "_readers",
        "_readers_requested",
        "_ready",
        "_writer_lock",
    )

    #: ``acquire`` understands the ``readonly`` keyword (see ``RawUnitOfWork``).
    supports_readonly_acquire = True

    def __init__(
        self,
        connection: Any,
        *,
        readers: int = 4,
        busy_timeout: float = 5.0,
        foreign_keys: bool = True,
    ) -> None:
        self._connection = SQLiteConnection(connection)
        self._readers_requested = max(0, int(readers))
        self._busy_timeout = busy_timeout
        self._foreign_keys = foreign_keys
        self._writer_lock = asyncio.Lock()
        self._readers: asyncio.Queue[SQLiteConnection] = asyncio.Queue()
        self._path = ""
        self._ready = False

    @property
    def dialect(self) -> sql.Dialect:
        """The dialect of this pool (SQLite)."""
        return sql.SQLITE

    @property
    def readers(self) -> int:
        """How many read-only connections are idle right now (0 for ``:memory:``)."""
        return self._readers.qsize()

    async def _ping_pragma(self, connection: Any, statement: str) -> list[Any]:
        """Run a PRAGMA and drain its cursor: some pragmas return a row."""
        cursor = await connection.execute(statement)
        try:
            return await cursor.fetchall()
        finally:
            await cursor.close()

    async def _configure(self, connection: Any, *, query_only: bool) -> None:
        """Apply per-connection pragmas — each SQLite connection starts with defaults."""
        await self._ping_pragma(connection, f"PRAGMA busy_timeout = {int(self._busy_timeout * 1000)}")
        if self._foreign_keys:
            await self._ping_pragma(connection, "PRAGMA foreign_keys = ON")
        if self._path:
            # WAL is a property of the file: readers may run during a write.
            await self._ping_pragma(connection, "PRAGMA journal_mode = WAL")
        if query_only:
            await self._ping_pragma(connection, "PRAGMA query_only = ON")

    async def _ensure_ready(self) -> None:
        """Open the database file, configure the writer and start the readers."""
        if self._ready:
            return
        rows = await self._ping_pragma(self._connection.raw, "PRAGMA database_list")
        # (seq, name, file) — the file is empty for an in-memory database
        self._path = str(rows[0][2]) if rows else ""
        await self._configure(self._connection.raw, query_only=False)

        if self._path and self._readers_requested:
            aiosqlite = _require("aiosqlite", "sqlite")
            for _ in range(self._readers_requested):
                raw = await aiosqlite.connect(self._path, isolation_level=None)
                try:
                    await self._configure(raw, query_only=True)
                except BaseException:
                    await raw.close()
                    raise
                self._readers.put_nowait(SQLiteConnection(raw))
        self._ready = True

    async def acquire(self, *, readonly: bool = False) -> SQLiteConnection:
        """Take a connection: a reader for read-only units, else the writer slot.

        Args:
            readonly: Hand out a read-only connection instead of the writer.

        Returns:
            A :class:`SQLiteConnection` bound to this pool.
        """
        await self._ensure_ready()
        if readonly and self._path and self._readers_requested:
            # wait for a reader instead of falling back to the writer: a reader
            # must never end up inside the writer's transaction
            return await self._readers.get()
        await self._writer_lock.acquire()
        return self._connection

    async def release(self, connection: SQLiteConnection) -> None:
        """Return a connection: the writer slot is freed, a reader goes back to the queue."""
        if connection is self._connection:
            if self._writer_lock.locked():
                self._writer_lock.release()
            return
        self._readers.put_nowait(connection)

    async def close(self) -> None:
        """Close the writer and every reader."""
        while not self._readers.empty():
            await self._readers.get_nowait().close()
        await self._connection.close()


# ── Factory ──────────────────────────────────────────────────────────────────


async def open_pool(
    dsn: str,
    *,
    min_size: int = 1,
    max_size: int = 10,
    sqlite_readers: int = 4,
    sqlite_busy_timeout: float = 5.0,
    sqlite_foreign_keys: bool = True,
) -> Any:
    """Open a native pool for an ``mysql://`` or ``sqlite://`` DSN.

    The driver is imported here, not at module import time, so that the optional
    extras stay optional.

    Args:
        dsn: Connection string; the scheme picks the driver.
        min_size: Minimum number of pooled connections (MySQL).
        max_size: Maximum number of pooled connections (MySQL).
        sqlite_readers: Extra read-only connections for the SQLite pool (WAL);
            ``0`` serves read-only units from the writer slot too.
        sqlite_busy_timeout: SQLite writer wait, in seconds, for a lock held by
            another process.
        sqlite_foreign_keys: Enable ``PRAGMA foreign_keys`` on SQLite connections.

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
        return SQLitePool(
            connection,
            readers=sqlite_readers,
            busy_timeout=sqlite_busy_timeout,
            foreign_keys=sqlite_foreign_keys,
        )
    raise ValueError(f"Unknown DSN scheme: {scheme!r} (expected mysql or sqlite)")
