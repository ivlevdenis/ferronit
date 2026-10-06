"""Postgres adapter tests — using SQLite for portability."""

import pytest
from sqlalchemy import Column, Integer, String
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ferrox.contrib.db import Base, RelationalUnitOfWork


class _UserModel(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)


@pytest.fixture
async def uow():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    uow = RelationalUnitOfWork(factory)
    async with uow:
        yield uow
    await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_repo_save_and_get(uow):
    repo = uow[_UserModel]
    user = _UserModel(name="Denis")
    await repo.save(user)
    await uow.commit()

    found = await repo.get(user.id)
    assert found is not None
    assert found.name == "Denis"


@pytest.mark.asyncio
async def test_postgres_repo_list(uow):
    repo = uow[_UserModel]
    await repo.save(_UserModel(name="A"))
    await repo.save(_UserModel(name="B"))
    await uow.commit()

    users = await repo.list()
    assert len(users) == 2


@pytest.mark.asyncio
async def test_postgres_uow_rollback(uow):
    repo = uow[_UserModel]
    await repo.save(_UserModel(name="Temp"))
    await uow.rollback()

    found = await repo.get(1)
    assert found is None
