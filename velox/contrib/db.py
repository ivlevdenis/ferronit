"""PostgreSQL adapter — SQLAlchemy async repository and Unit of Work."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from velox.hexagonal import Adapter, UnitOfWork

__all__ = [
    "Base",
    "RelationalRepository",
    "CoreRepository",
    "RelationalUnitOfWork",
    "create_relational_uow",
]


class Base(DeclarativeBase):
    """Base class for SQLAlchemy ORM models."""
    pass


class RelationalRepository(Adapter):
    """Repository backed by SQLAlchemy async session."""

    __slots__ = ("_session", "_model")

    def __init__(self, session: AsyncSession, model: type):
        self._session = session
        self._model = model

    async def get(self, id) -> object | None:
        return await self._session.get(self._model, id)

    async def save(self, entity) -> None:
        self._session.add(entity)

    async def list(self, *filters, limit: int = 100, offset: int = 0):
        from sqlalchemy import select

        stmt = select(self._model).filter(*filters).limit(limit).offset(offset)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def delete(self, entity) -> None:
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
        return list(self._table.primary_key.columns)[0]

    async def get(self, id) -> dict | None:
        from sqlalchemy import select

        pk = self._pk()
        stmt = select(self._table).where(pk == id)
        result = await self._session.execute(stmt)
        row = result.mappings().first()
        return dict(row) if row else None

    async def save(self, data: dict, *, returning: bool = False) -> dict:
        """INSERT; returns data + generated PK.

        With `returning=True` the full row is returned via INSERT...RETURNING
        (includes server_defaults and triggers; slightly slower).
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
            return dict(result.mappings().first())

        result = await self._session.execute(insert(self._table).values(**data))
        if pk.name in data:
            return dict(data)
        return {**data, pk.name: result.inserted_primary_key[0]}

    async def update(self, id, data: dict) -> dict:
        """UPDATE by primary key; returns merged dict."""
        from sqlalchemy import update

        pk = self._pk()
        stmt = update(self._table).where(pk == id).values(**data)
        await self._session.execute(stmt)
        return {**data, pk.name: id}

    async def list(self, *filters, limit: int = 100, offset: int = 0) -> list[dict]:
        from sqlalchemy import select

        stmt = select(self._table).filter(*filters).limit(limit).offset(offset)
        result = await self._session.execute(stmt)
        return [dict(r) for r in result.mappings().all()]

    async def delete(self, id) -> None:
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

    __slots__ = ("_session_factory", "_session")

    def __init__(self, session_factory: async_sessionmaker):
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("No active session")
        return self._session

    def __getitem__(self, model) -> RelationalRepository | CoreRepository:
        from sqlalchemy.sql.schema import Table as SATable

        if isinstance(model, SATable):
            return CoreRepository(self.session, model)
        return RelationalRepository(self.session, model)

    async def commit(self) -> None:
        if self._session:
            await self._session.commit()

    async def rollback(self) -> None:
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
    """Create a Unit of Work connected to a PostgreSQL database."""
    engine = create_async_engine(database_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return RelationalUnitOfWork(factory)
