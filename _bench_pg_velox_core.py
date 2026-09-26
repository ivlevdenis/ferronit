
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

engine = create_async_engine("postgresql+asyncpg://postgres:postgres@localhost:5432/postgres", echo=False)
factory = async_sessionmaker(engine, expire_on_commit=False)


def init_db():
    import asyncio

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
        async with factory() as session:
            from sqlalchemy import func, select

            count = (await session.execute(select(func.count()).select_from(users))).scalar_one()
            if count == 0:
                for i in range(100):
                    await session.execute(insert(users).values(name=f"user_{i}", email=f"u{i}@example.com"))
                await session.commit()
            await engine.dispose()

    asyncio.run(_init())


init_db()

app = Velox()


@app.route("/users")
async def list_users(req):
    async with factory() as session:
        result = await session.execute(select(users).limit(100))
        rows = [dict(r) for r in result.mappings().all()]
    return {"users": rows}


@app.route("/users", methods=["POST"])
async def create_user(req):
    body = await req.json()
    async with factory() as session:
        result = await session.execute(
            insert(users).values(name=body["name"], email=body["email"])
        )
        row_id = result.inserted_primary_key[0]
        await session.commit()
    return {"id": row_id, "name": body["name"]}
