"""PostgreSQL adapter — SQLAlchemy async repository and Unit of Work."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from ferrox.hexagonal import Adapter, UnitOfWork

__all__ = [
    "Base",
    "CoreRepository",
    "RelationalRepository",
    "RelationalUnitOfWork",
    "create_relational_uow",
]


class Base(DeclarativeBase):
    """Base class for SQLAlchemy ORM models."""
    pass


class RelationalRepository(Adapter):
    """Repository backed by SQLAlchemy async session."""

    __slots__ = ("_model", "_session")

    def __init__(self, session: AsyncSession, model: type):
        self._session = session
        self._model = model

    async def get(self, id) -> object | None:
        """Fetch a single ORM entity by primary key.

        Args:
            id: Primary key value.

        Returns:
            The ORM entity, or ``None`` if no row matches.
        """
        return await self._session.get(self._model, id)

    async def save(self, entity) -> None:
        """Stage an ORM entity for insert or update.

        The entity is added to the active session; the row is flushed and
        committed when the surrounding Unit of Work exits successfully.

        Args:
            entity: Pending or detached ORM instance to persist.
        """
        self._session.add(entity)

    async def list(self, *filters, limit: int = 100, offset: int = 0):
        """List ORM entities, optionally filtered.

        Args:
            *filters: SQLAlchemy filter expressions applied with ``.filter()``.
            limit: Maximum number of rows to return.
            offset: Number of rows to skip.

        Returns:
            A list of ORM entities (possibly empty).
        """
        from sqlalchemy import select

        stmt: Any = select(self._model).filter(*filters).limit(limit).offset(offset)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def delete(self, entity) -> None:
        """Mark an ORM entity for deletion.

        Args:
            entity: ORM instance to remove on the next flush/commit.
        """
        await self._session.delete(entity)


class CoreRepository(Adapter):
    """SQLAlchemy Core repository — rows in, dicts out (no ORM mapping).

    Usage:
        users = Table("users", metadata, Column("id", Integer, primary_key=True), ...)

        async with uow:
            repo = uow[users]            # Table → CoreRepository
            await repo.save({"name": "A"})
            row = await repo.get(1)      # {"id": 1, "name": "A"} | None
            await repo.update(1, {"name": "B"})
            await repo.delete(1)
    """

    __slots__ = ("_session", "_table")

    def __init__(self, session: AsyncSession, table):
        self._session = session
        self._table = table

    def _pk(self):
        return next(iter(self._table.primary_key.columns))

    async def get(self, id) -> dict | None:
        """Fetch a single row by primary key as a dict.

        Args:
            id: Primary key value.

        Returns:
            The row as a dict, or ``None`` if no row matches.
        """
        from sqlalchemy import select

        pk = self._pk()
        stmt = select(self._table).where(pk == id)
        result = await self._session.execute(stmt)
        row = result.mappings().first()
        return dict(row) if row else None

    async def save(self, data: dict, *, returning: bool = False) -> dict:
        """INSERT and return the stored row as a dict.

        By default this uses the fast path and builds the result from the input
        columns plus the generated primary key (``cursor.inserted_primary_key``).
        Pass ``returning=True`` to use ``INSERT ... RETURNING`` instead: that
        returns the full server-side row, including ``server_default`` values
        and columns populated by triggers, but is slightly slower.

        Args:
            data: Column name → value mapping to insert.
            returning: Use ``INSERT ... RETURNING`` to read back the full row.

        Returns:
            The inserted row as a dict. With ``returning=True`` and a database
            that lacks ``RETURNING`` support (e.g. legacy MySQL), falls back to
            the input ``data``.
        """
        from sqlalchemy import insert

        pk = self._pk()
        if returning:
            stmt = insert(self._table).values(**data).returning(*self._table.c)
            try:
                result = await self._session.execute(stmt)
            except Exception:
                # DB without RETURNING support (legacy MySQL etc.)
                await self._session.execute(insert(self._table).values(**data))
                return dict(data)
            row = result.mappings().first()
            return dict(row) if row is not None else dict(data)

        result = await self._session.execute(insert(self._table).values(**data))
        if pk.name in data:
            return dict(data)
        pk_value = getattr(result, "inserted_primary_key", None)
        return {**data, pk.name: pk_value[0] if pk_value else None}

    async def update(self, id, data: dict) -> dict:
        """UPDATE by primary key; returns merged dict."""
        from sqlalchemy import update

        pk = self._pk()
        stmt = update(self._table).where(pk == id).values(**data)
        await self._session.execute(stmt)
        return {**data, pk.name: id}

    async def list(self, *filters, limit: int = 100, offset: int = 0) -> list[dict]:
        """List rows as dicts, optionally filtered and paginated.

        Args:
            *filters: SQLAlchemy filter expressions applied with ``.filter()``.
            limit: Maximum number of rows to return.
            offset: Number of rows to skip.

        Returns:
            A list of rows, each as a dict (possibly empty).
        """
        from sqlalchemy import select

        stmt = select(self._table).filter(*filters).limit(limit).offset(offset)
        result = await self._session.execute(stmt)
        return [dict(r) for r in result.mappings().all()]

    async def delete(self, id) -> None:
        """Delete a row by primary key.

        Args:
            id: Primary key value of the row to delete.
        """
        from sqlalchemy import delete as sa_delete

        pk = self._pk()
        stmt = sa_delete(self._table).where(pk == id)
        await self._session.execute(stmt)


class RelationalUnitOfWork(UnitOfWork):
    """Unit of Work — auto-commit on exit.

    Usage:
        async with uow:
            repo = uow[UserModel]
            await repo.save(user)
            # auto-committed on exit
    """

    __slots__ = ("_session", "_session_factory")

    def __init__(self, session_factory: async_sessionmaker):
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    @property
    def session(self) -> AsyncSession:
        """Active SQLAlchemy session for the current Unit of Work context.

        Returns:
            The session opened by ``__aenter__``.

        Raises:
            RuntimeError: If accessed outside an ``async with`` block.
        """
        if self._session is None:
            raise RuntimeError("No active session")
        return self._session

    def __getitem__(self, model) -> RelationalRepository | CoreRepository:
        from sqlalchemy.sql.schema import Table as SATable

        if isinstance(model, SATable):
            return CoreRepository(self.session, model)
        return RelationalRepository(self.session, model)

    async def commit(self) -> None:
        """Commit the current transaction, if a session is open."""
        if self._session:
            await self._session.commit()

    async def rollback(self) -> None:
        """Roll back the current transaction, if a session is open."""
        if self._session:
            await self._session.rollback()

    async def __aenter__(self):
        self._session = self._session_factory()
        return self

    async def __aexit__(self, *args):
        if self._session:
            try:
                if args[0] is None:
                    await self._session.commit()
                else:
                    await self._session.rollback()
            finally:
                await self._session.close()
                self._session = None


def create_relational_uow(database_url: str) -> RelationalUnitOfWork:
    """Create a Unit of Work bound to a PostgreSQL database.

    Args:
        database_url: SQLAlchemy async URL (e.g. ``postgresql+asyncpg://...``).

    Returns:
        A ``RelationalUnitOfWork`` with its own async engine and session factory.
    """
    engine = create_async_engine(database_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return RelationalUnitOfWork(factory)
