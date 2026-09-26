
from velox import Velox
from sqlalchemy import Column, Integer, MetaData, String, Table, insert, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

metadata = MetaData()
users = Table(
    "users", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String, nullable=False),
    Column("email", String, nullable=False),
)
engine = create_async_engine("sqlite+aiosqlite://", echo=False)
factory = async_sessionmaker(engine, expire_on_commit=False)


def init_db():
    import asyncio
    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
        async with factory() as session:
            for i in range(100):
                await session.execute(insert(users).values(name=f"user_{i}", email=f"u{i}@example.com"))
            await session.commit()
    asyncio.run(_init())


init_db()
app = Velox()


@app.route("/users")
async def list_users(req):
    async with factory() as session:
        result = await session.execute(select(users))
        rows = [dict(r) for r in result.mappings().all()]
    return {"users": rows}
