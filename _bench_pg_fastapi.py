
from fastapi import FastAPI

from sqlalchemy import Column, Integer, String
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False)


engine = create_async_engine("postgresql+asyncpg://postgres:postgres@localhost:5432/postgres", echo=False)
factory = async_sessionmaker(engine, expire_on_commit=False)


def init_db():
    import asyncio

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            from sqlalchemy import func, select

            count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
            if count == 0:
                for i in range(100):
                    session.add(User(name=f"user_{i}", email=f"u{i}@example.com"))
                await session.commit()
            await engine.dispose()

    asyncio.run(_init())


init_db()


app = FastAPI()


@app.get("/users")
async def list_users():
    from sqlalchemy import select

    async with factory() as session:
        result = await session.execute(select(User).limit(100))
        users = result.scalars().all()
    return {
        "users": [
            {"id": u.id, "name": u.name, "email": u.email} for u in users
        ]
    }


@app.post("/users")
async def create_user(body: dict):
    async with factory() as session:
        user = User(name=body["name"], email=body["email"])
        session.add(user)
        await session.commit()
    return {"id": user.id, "name": user.name}
