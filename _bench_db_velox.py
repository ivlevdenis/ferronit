
from velox import Velox

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


engine = create_async_engine("sqlite+aiosqlite://", echo=False)
factory = async_sessionmaker(engine, expire_on_commit=False)


def init_db():
    import asyncio

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            for i in range(100):
                session.add(User(name=f"user_{i}", email=f"u{i}@example.com"))
            await session.commit()

    asyncio.run(_init())


init_db()


app = Velox()


@app.route("/users")
async def list_users(req):
    from sqlalchemy import select

    async with factory() as session:
        result = await session.execute(select(User))
        users = result.scalars().all()
    return {
        "users": [
            {"id": u.id, "name": u.name, "email": u.email} for u in users
        ]
    }


@app.route("/users", methods=["POST"])
async def create_user(req):
    body = await req.json()
    async with factory() as session:
        user = User(name=body["name"], email=body["email"])
        session.add(user)
        await session.commit()
    return {"id": user.id, "name": user.name}
