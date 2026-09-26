
from velox import Velox
from velox.contrib.db import CoreRepository

from sqlalchemy import Column, Integer, String
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import Column as _C, Integer as _I, MetaData, String as _S, Table

metadata = MetaData()
users = Table(
    "users", metadata,
    _C("id", _I, primary_key=True, autoincrement=True),
    _C("name", _S, nullable=False),
    _C("email", _S, nullable=False),
)
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False)


engine = create_async_engine("sqlite+aiosqlite://", echo=False)
factory = async_sessionmaker(engine, expire_on_commit=False)


def init_db():
    import asyncio

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            from sqlalchemy import insert
            for i in range(100):
                await session.execute(insert(users).values(name=f"user_{i}", email=f"u{i}@example.com"))
            await session.commit()

    asyncio.run(_init())


init_db()
