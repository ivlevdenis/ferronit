"""SQLite pool: one writer slot plus WAL readers.

What is checked here is the concurrency contract, not SQL generation:

* write units never overlap (no ``cannot start a transaction within a
  transaction`` / ``database is locked``);
* read-only units borrow their own connections and see committed data only;
* the writer slot is exclusive and is always released, including on error;
* per-connection pragmas land on every connection (WAL, foreign keys, read-only).
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from ferronit.contrib.rawdb import create_pool

aiosqlite = pytest.importorskip("aiosqlite")

DDL = (
    "CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT NOT NULL)",
    "CREATE TABLE child ("
    "id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL REFERENCES parent(id), "
    "name TEXT NOT NULL)",
)


@pytest.fixture
async def pool(tmp_path):
    created = await create_pool(f"sqlite://{tmp_path / 'app.db'}", sqlite_readers=2)
    async with created.uow() as uow:
        for statement in DDL:
            await uow.connection.execute(statement)
    yield created
    await created.close()


async def test_concurrent_write_units_all_succeed(pool) -> None:
    """Five parallel write units: serialised, no lock errors, all rows land."""

    async def insert(tag: str) -> None:
        async with pool.uow() as uow:
            await uow["parent"].save({"name": tag})

    await asyncio.gather(*[insert(f"p{i}") for i in range(5)])

    async with pool.uow(readonly=True) as uow:
        rows = await uow["parent"].list()

    assert sorted(row["name"] for row in rows) == ["p0", "p1", "p2", "p3", "p4"]


async def test_read_units_take_their_own_connections(pool) -> None:
    """Two read-only units run at the same time on different connections."""
    writer = pool._pool._connection
    async with pool.uow(readonly=True) as first, pool.uow(readonly=True) as second:
        assert first.connection is not second.connection
        assert first.connection is not writer
        assert reader_count(pool) == 0  # both readers are checked out
    assert reader_count(pool) == 2


def reader_count(pool) -> int:
    """Idle read-only connections in the SQLite pool."""
    return pool._pool.readers


async def test_readers_see_committed_rows_only(pool) -> None:
    """A write inside an open unit is invisible to readers until it commits."""
    async with pool.uow() as writer:
        await writer["parent"].save({"name": "pending"})
        async with pool.uow(readonly=True) as reader:
            assert await reader["parent"].list() == []

    async with pool.uow(readonly=True) as reader:
        assert [row["name"] for row in await reader["parent"].list()] == ["pending"]


async def test_read_only_unit_cannot_write(pool) -> None:
    """Readers are opened with ``PRAGMA query_only`` — a write fails loudly."""
    async with pool.uow(readonly=True) as uow:
        with pytest.raises(sqlite3.OperationalError) as caught:
            await uow["parent"].save({"name": "nope"})

    message = str(caught.value).lower()
    assert "read" in message and "only" in message


async def test_writer_slot_is_exclusive(pool) -> None:
    """A second write unit waits for the first one to leave the slot."""
    started = asyncio.Event()

    async def second() -> None:
        async with pool.uow() as uow:
            started.set()
            await uow["parent"].save({"name": "second"})

    async with pool.uow():
        task = asyncio.create_task(second())
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(started.wait(), timeout=0.2)

    await asyncio.wait_for(task, timeout=5)
    assert started.is_set()


async def test_failed_unit_releases_the_writer(pool) -> None:
    """An exception in the body rolls back and frees the slot for the next unit."""
    with pytest.raises(RuntimeError):
        async with pool.uow() as uow:
            await uow["parent"].save({"name": "rolled-back"})
            raise RuntimeError("boom")

    async with pool.uow() as uow:
        await uow["parent"].save({"name": "after"})

    async with pool.uow(readonly=True) as uow:
        assert [row["name"] for row in await uow["parent"].list()] == ["after"]


async def test_foreign_keys_are_enforced(pool) -> None:
    """``PRAGMA foreign_keys`` is on for every connection — no orphan rows."""
    async with pool.uow() as uow:
        with pytest.raises(sqlite3.IntegrityError):
            await uow["child"].save({"parent_id": 999, "name": "orphan"})


async def test_file_database_runs_in_wal(pool) -> None:
    """WAL is what makes parallel reads possible; check it on the writer."""
    async with pool.uow(readonly=True) as uow:
        mode = await uow.connection.fetchval("PRAGMA journal_mode")

    assert str(mode).lower() == "wal"


async def test_in_memory_database_gets_the_writer_only() -> None:
    """A private in-memory database cannot be shared: single connection, no readers."""
    created = await create_pool("sqlite://:memory:")
    try:
        async with created.uow() as uow:
            await uow.connection.execute(DDL[0])
            await uow["parent"].save({"name": "memory"})

        async with created.uow(readonly=True) as uow:
            assert [row["name"] for row in await uow["parent"].list()] == ["memory"]

        assert created._pool.readers == 0
    finally:
        await created.close()
