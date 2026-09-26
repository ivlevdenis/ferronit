"""Чистые замеры SQLite без прослоек: сколько выдаёт сама БД.

SELECT — 100 строк (как GET /users), INSERT + commit (как POST /users).
Сравнение: sqlite3 (sync C-драйвер) vs aiosqlite (async, как в приложении).
"""
import asyncio
import sqlite3
import time

N = 20000


def make_sync_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT)")
    conn.executemany(
        "INSERT INTO users (name, email) VALUES (?, ?)",
        [(f"user_{i}", f"u{i}@example.com") for i in range(100)],
    )
    conn.commit()
    return conn


def bench(fn, n=N, warmup=2000):
    for _ in range(warmup):
        fn()
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    dt = time.perf_counter() - t0
    return n / dt


def main():
    # ── sqlite3 (sync C-драйвер) ──
    conn = make_sync_db()

    def select_sync():
        cur = conn.execute("SELECT id, name, email FROM users")
        return cur.fetchall()

    def insert_sync():
        conn.execute("INSERT INTO users (name, email) VALUES (?, ?)", ("bench", "bench@example.com"))
        conn.commit()

    rps_select = bench(select_sync)
    rps_insert = bench(insert_sync)
    print(f"sqlite3 (sync)   SELECT 100 строк: {rps_select:10,.0f} ops/s")
    print(f"sqlite3 (sync)   INSERT+commit:    {rps_insert:10,.0f} ops/s")

    # ── aiosqlite (async, как в приложении) ──
    async def make_async_db():
        import aiosqlite
        db = await aiosqlite.connect(":memory:")
        await db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT)")
        await db.executemany(
            "INSERT INTO users (name, email) VALUES (?, ?)",
            [(f"user_{i}", f"u{i}@example.com") for i in range(100)],
        )
        await db.commit()
        return db

    async def bench_async():
        import aiosqlite
        db = await make_async_db()

        async def select_async():
            cur = await db.execute("SELECT id, name, email FROM users")
            return await cur.fetchall()

        async def insert_async():
            await db.execute("INSERT INTO users (name, email) VALUES (?, ?)", ("bench", "bench@example.com"))
            await db.commit()

        async def run(fn, n=N, warmup=2000):
            for _ in range(warmup):
                await fn()
            t0 = time.perf_counter()
            for _ in range(n):
                await fn()
            return n / (time.perf_counter() - t0)

        s = await run(select_async)
        i = await run(insert_async)
        print(f"aiosqlite (async) SELECT 100 строк: {s:10,.0f} ops/s")
        print(f"aiosqlite (async) INSERT+commit:    {i:10,.0f} ops/s")
        await db.close()

    asyncio.run(bench_async())

    # ── SQLAlchemy async (как в приложении, но без фреймворка/HTTP) ──
    async def bench_sqlalchemy():
        from sqlalchemy import Column, Integer, String, select
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
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            for i in range(100):
                session.add(User(name=f"user_{i}", email=f"u{i}@example.com"))
            await session.commit()

        async def select_orm():
            async with factory() as session:
                result = await session.execute(select(User))
                return result.scalars().all()

        async def insert_orm():
            async with factory() as session:
                session.add(User(name="bench", email="bench@example.com"))
                await session.commit()

        async def run(fn, n=5000, warmup=500):
            for _ in range(warmup):
                await fn()
            t0 = time.perf_counter()
            for _ in range(n):
                await fn()
            return n / (time.perf_counter() - t0)

        s = await run(select_orm)
        i = await run(insert_orm)
        print(f"SQLAlchemy async  SELECT 100 строк: {s:10,.0f} ops/s")
        print(f"SQLAlchemy async  INSERT+commit:    {i:10,.0f} ops/s")
        await engine.dispose()

    asyncio.run(bench_sqlalchemy())

    # ── SQLAlchemy Core (как новый velox CoreRepository, без фреймворка) ──
    async def bench_core():
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
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
        async with factory() as session:
            for i in range(100):
                await session.execute(insert(users).values(name=f"user_{i}", email=f"u{i}@example.com"))
            await session.commit()

        async def select_core():
            async with factory() as session:
                result = await session.execute(select(users))
                return [dict(r) for r in result.mappings().all()]

        async def insert_core():
            async with factory() as session:
                await session.execute(insert(users).values(name="bench", email="bench@example.com"))
                await session.commit()

        async def run(fn, n=20000, warmup=2000):
            for _ in range(warmup):
                await fn()
            t0 = time.perf_counter()
            for _ in range(n):
                await fn()
            return n / (time.perf_counter() - t0)

        s = await run(select_core)
        i = await run(insert_core)
        print(f"SQLAlchemy Core    SELECT 100 строк: {s:10,.0f} ops/s")
        print(f"SQLAlchemy Core    INSERT+commit:    {i:10,.0f} ops/s")
        await engine.dispose()

    asyncio.run(bench_core())

    print()
    print("Для сравнения (бенч с БД):")
    print(f"  Velox  GET /users на granian: 1 644 req/s")
    print(f"  Velox  POST /users на granian: 4 064 req/s")


if __name__ == "__main__":
    main()
