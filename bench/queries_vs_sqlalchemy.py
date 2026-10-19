"""Прямое сравнение слоёв доступа к PostgreSQL: Ferronit против SQLAlchemy.

Мерит один и тот же набор запросов к таблице ``bench_rows`` на каждом слое
доступа, в одном процессе и одном event loop, 1 клиент (concurrency=1):

    get_1     -- SELECT одной строки по pk
    list_100  -- SELECT 100 строк
    insert_1  -- INSERT одной строки
    update_1  -- UPDATE одной строки по pk
    upsert_1  -- INSERT ... ON CONFLICT DO UPDATE одной строки
    delete_1  -- DELETE одной строки по pk

Слои:
    ferronit-rawmodel  -- типизированные модели + query DSL (RawModelRepository)
    ferronit-rawdb     -- dict-строки, явный SQL (RawRepository)
    sqlalchemy-core    -- SQLAlchemy Core (select/insert/update/delete)
    sqlalchemy-orm     -- SQLAlchemy ORM (сущности, identity map сессии)
    asyncpg            -- сырой драйвер, ориентир «дна» (не SQLAlchemy)

Методика: замеры вперемешку (round-robin по слоям внутри прогона), лучшее из
``RUNS`` прогонов, все числа -- в запросах в секунду. Транзакция на запрос там,
где её открывает сам слой: ``uow()`` у Ferronit, ``begin()``/сессия у SQLAlchemy,
автокоммит оператора у сырого asyncpg; чтение у Ferronit идёт через
``uow(readonly=True)`` (BEGIN не выдаётся). Перед каждым прогоном таблица
восстанавливается вне тайминга (``TRUNCATE`` + N строк) и на ней выключается
autovacuum: без этого фоновый vacuum, запускающийся после ~20% мёртвых строк,
забирает I/O прямо во время замера и роняет записи с ~930 до ~330 запросов/с
(проверено прямым замером). Перед каждой операцией -- ``CHECKPOINT`` и пауза
(``BENCH_PAUSE`` с): первый замер после тяжёлой фазы идёт с остаточным I/O диска
и втрое медленнее (319 против 939). Настройка возвращается в конце прогона.

Запуск: .venv/bin/python bench/queries_vs_sqlalchemy.py
Требует PostgreSQL на :5432 (таблицу создаёт сам).
"""

from __future__ import annotations

import asyncio
import os
import platform
import time
from datetime import datetime

DSN = os.environ.get("BENCH_DSN", "postgresql://postgres:postgres@127.0.0.1:5432/postgres")
PQ = DSN.replace("postgresql://", "postgresql+asyncpg://")  # для SQLAlchemy

NAME = "bench"
EMAIL = "bench@example.com"


def upd_name(i: int) -> str:
    """Значение ``name`` для i-й итерации UPDATE/UPSERT.

    Меняется каждую итерацию намеренно: при записи того же значения ORM
    (dirty-tracking) не отправил бы UPDATE вообще, и замер превратился бы в
    замер пустого flush.
    """
    return f"bench_{i}"


def upd_email(i: int) -> str:
    """Значение ``email`` для i-й итерации UPDATE/UPSERT (см. ``upd_name``)."""
    return f"b{i}@example.com"

SEED_ROWS = 1000
DELETE_ROWS = 5000          # столько строк нужно, чтобы DELETE бил по существующим
RUNS = 3                    # лучшее из N
ITERS = {
    "get_1": 3000,
    "list_100": 1500,
    "insert_1": 3000,
    "update_1": 3000,
    "upsert_1": 3000,
    "delete_1": 3000,
}
READ_OPS = ("get_1", "list_100")
OPS = ("get_1", "list_100", "insert_1", "update_1", "upsert_1", "delete_1")

# BENCH_OPS=upsert_1,delete_1 — прогнать только выбранные операции.
_bench_ops = os.environ.get("BENCH_OPS")
if _bench_ops:
    _wanted_ops = tuple(op.strip() for op in _bench_ops.split(",") if op.strip())
    OPS = tuple(op for op in OPS if op in _wanted_ops)

if os.environ.get("BENCH_QUICK"):     # смоук: тот же код, короткие прогоны
    RUNS = 1
    ITERS = {k: max(50, v // 10) for k, v in ITERS.items()}

PAUSE = float(os.environ.get("BENCH_PAUSE", "5"))   # с, перед каждой операцией

# ── схема таблицы: одна на всех ───────────────────────────────────────
CREATE = (
    "CREATE TABLE IF NOT EXISTS bench_rows ("
    "id serial PRIMARY KEY, name text NOT NULL, email text NOT NULL)"
)

# ORM-модель объявлена на уровне модуля: SQLAlchemy резолвит аннотации
# ``Mapped[...]`` по globals модуля, а не по locals функции.
from sqlalchemy import String as SAString  # noqa: E402
from sqlalchemy.orm import DeclarativeBase as _DeclarativeBase  # noqa: E402
from sqlalchemy.orm import Mapped, mapped_column  # noqa: E402


class OrmBase(_DeclarativeBase):
    """База декларативных ORM-моделей стенда."""


class OrmRow(OrmBase):
    """Строка ``bench_rows`` как ORM-сущность (для слоя sqlalchemy-orm)."""

    __tablename__ = "bench_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(SAString, nullable=False)
    email: Mapped[str] = mapped_column(SAString, nullable=False)


async def seed(rows: int) -> None:
    """Восстановить таблицу: TRUNCATE + ``rows`` строк с id 1..rows (вне тайминга).

    Заодно отключает autovacuum на ``bench_rows``: без этого фоновый vacuum,
    запускающийся после ~20% мёртвых строк (прогоны оставляют десятки тысяч
    удалённых/переписанных версий), забирает I/O прямо во время замера — в кейсе
    окт 2026 записи падали с ~930 до ~330 запросов/с. Условие одинаково для всех
    слоёв; настройка снимается в конце прогона.
    """
    import asyncpg

    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute(CREATE)
        await conn.execute("ALTER TABLE bench_rows SET (autovacuum_enabled = false)")
        await conn.execute("TRUNCATE bench_rows RESTART IDENTITY")
        await conn.execute(
            "INSERT INTO bench_rows (name, email) "
            "SELECT 'user_' || g, 'u' || g || '@example.com' "
            "FROM generate_series(1, $1) AS g",
            rows,
        )
    finally:
        await conn.close()


def seed_rows_for(op: str) -> int:
    """Сколько строк нужно в таблице под операцию."""
    return DELETE_ROWS if op == "delete_1" else SEED_ROWS


# ── слои доступа ──────────────────────────────────────────────────────
class Variant:
    """Один слой доступа: свой пул/движок и один и тот же набор запросов."""

    name = "?"
    ops: tuple[str, ...] = OPS      # операции, которые слой реально поддерживает

    async def setup(self) -> None:  # pragma: no cover - интерфейс
        """Открыть пул/движок (внутри event loop)."""

    async def close(self) -> None:
        """Закрыть пул/движок."""

    async def run(self, op: str, i: int) -> object:
        """Выполнить один запрос ``op`` (счётчик ``i`` -- для delete).

        Args:
            op: Имя операции из ``ITERS``.
            i: Номер итерации внутри прогона.

        Returns:
            Результат запроса (не используется, нужен для отсутствия dead code).
        """
        raise NotImplementedError


class FerronitRawModel(Variant):
    """Ferronit: типизированные модели + query DSL поверх asyncpg."""

    name = "ferronit-rawmodel"

    async def setup(self) -> None:
        from ferronit.contrib.rawdb import create_pool

        self.pool = await create_pool(DSN, min_size=1, max_size=16)

    async def close(self) -> None:
        await self.pool.close()


class FerronitRawDb(Variant):
    """Ferronit: dict-строки и явный SQL (RawRepository).

    ``upsert`` у dict-слоя нет — он живёт в ``RawModelRepository``, поэтому
    эта клетка не меряется (в таблице -- прочерк).
    """

    name = "ferronit-rawdb"
    ops = tuple(op for op in OPS if op != "upsert_1")

    async def setup(self) -> None:
        from ferronit.contrib.rawdb import create_pool

        self.pool = await create_pool(DSN, min_size=1, max_size=16)

    async def close(self) -> None:
        await self.pool.close()


class SACore(Variant):
    """SQLAlchemy Core: выражения вместо SQL, dict-строки."""

    name = "sqlalchemy-core"

    async def setup(self) -> None:
        from sqlalchemy import Column, Integer, MetaData, String, Table
        from sqlalchemy.ext.asyncio import create_async_engine

        self.meta = MetaData()
        self.t = Table(
            "bench_rows",
            self.meta,
            Column("id", Integer, primary_key=True),
            Column("name", String, nullable=False),
            Column("email", String, nullable=False),
        )
        self.engine = create_async_engine(PQ, pool_size=16, max_overflow=0)

    async def close(self) -> None:
        await self.engine.dispose()

    async def run(self, op: str, i: int) -> object:
        from sqlalchemy import delete, insert, select, update
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        t = self.t
        if op in READ_OPS:
            async with self.engine.connect() as conn:
                if op == "get_1":
                    res = await conn.execute(select(t).where(t.c.id == 1))
                    return res.mappings().first()
                res = await conn.execute(select(t).limit(100))
                return res.mappings().all()
        async with self.engine.begin() as conn:
            if op == "insert_1":
                res = await conn.execute(
                    insert(t).values(name=NAME, email=EMAIL).returning(t.c.id)
                )
                return res.scalar()
            if op == "update_1":
                return await conn.execute(
                    update(t)
                    .where(t.c.id == 1)
                    .values(name=upd_name(i), email=upd_email(i))
                )
            if op == "upsert_1":
                stmt = (
                    pg_insert(t)
                    .values(id=1, name=upd_name(i), email=upd_email(i))
                    .on_conflict_do_update(
                        index_elements=[t.c.id],
                        set_={"name": upd_name(i), "email": upd_email(i)},
                    )
                    .returning(t.c.id)
                )
                res = await conn.execute(stmt)
                return res.scalar()
            return await conn.execute(delete(t).where(t.c.id == i % DELETE_ROWS + 1))


class SAOrm(Variant):
    """SQLAlchemy ORM: сущности и сессия с identity map."""

    name = "sqlalchemy-orm"

    async def setup(self) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        self.Row = OrmRow
        self.engine = create_async_engine(PQ, pool_size=16, max_overflow=0)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def close(self) -> None:
        await self.engine.dispose()

    async def run(self, op: str, i: int) -> object:
        from sqlalchemy import select
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        Row = self.Row
        if op == "get_1":
            async with self.factory() as session:
                return await session.get(Row, 1)
        if op == "list_100":
            async with self.factory() as session:
                res = await session.execute(select(Row).limit(100))
                return res.scalars().all()
        if op == "insert_1":
            async with self.factory() as session:
                row = Row(name=NAME, email=EMAIL)
                session.add(row)
                await session.flush()
                row_id = row.id
                await session.commit()
                return row_id
        if op == "update_1":
            async with self.factory() as session:
                row = await session.get(Row, 1)
                assert row is not None
                row.name = upd_name(i)
                row.email = upd_email(i)
                await session.commit()
                return row.id
        if op == "upsert_1":
            async with self.factory() as session:
                stmt = (
                    pg_insert(Row)
                    .values(id=1, name=upd_name(i), email=upd_email(i))
                    .on_conflict_do_update(
                        index_elements=[Row.id],
                        set_={"name": upd_name(i), "email": upd_email(i)},
                    )
                    .returning(Row.id)
                )
                res = await session.execute(stmt)
                row_id = res.scalars().first()
                await session.commit()
                return row_id
        async with self.factory() as session:
            row = await session.get(Row, i % DELETE_ROWS + 1)
            if row is not None:
                await session.delete(row)
                await session.commit()
            return row


class AsyncpgRaw(Variant):
    """Сырой asyncpg: тот же SQL, что держит репозиторий. Ориентир «дна»."""

    name = "asyncpg"

    async def setup(self) -> None:
        import asyncpg

        self.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=16)

    async def close(self) -> None:
        await self.pool.close()

    async def run(self, op: str, i: int) -> object:
        async with self.pool.acquire() as conn:
            if op == "get_1":
                return await conn.fetchrow(
                    "SELECT id, name, email FROM bench_rows WHERE id = $1", 1
                )
            if op == "list_100":
                return await conn.fetch("SELECT id, name, email FROM bench_rows LIMIT $1", 100)
            if op == "insert_1":
                return await conn.fetchval(
                    "INSERT INTO bench_rows (name, email) VALUES ($1, $2) RETURNING id",
                    NAME,
                    EMAIL,
                )
            if op == "update_1":
                return await conn.execute(
                    "UPDATE bench_rows SET name = $1, email = $2 WHERE id = $3",
                    upd_name(i),
                    upd_email(i),
                    1,
                )
            if op == "upsert_1":
                return await conn.fetchval(
                    "INSERT INTO bench_rows (id, name, email) VALUES ($1, $2, $3) "
                    "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, "
                    "email = EXCLUDED.email RETURNING id",
                    1,
                    upd_name(i),
                    upd_email(i),
                )
            return await conn.execute(
                "DELETE FROM bench_rows WHERE id = $1", i % DELETE_ROWS + 1
            )


def build_variants() -> list[Variant]:
    """Собрать слои в порядке вывода (Ferronit первым)."""
    from ferronit.contrib.rawmodel import Model, select

    class BenchRow(Model):
        __table__ = "bench_rows"
        id: int = 0
        name: str = ""
        email: str = ""

    class RawModel(FerronitRawModel):
        async def run(self, op: str, i: int) -> object:
            if op in READ_OPS:
                async with self.pool.uow(readonly=True) as uow:
                    repo = uow.model(BenchRow)
                    if op == "get_1":
                        return await repo.get(1)
                    return await repo.list(limit=100)
            async with self.pool.uow() as uow:
                repo = uow.model(BenchRow)
                if op == "insert_1":
                    return await repo.save(BenchRow(name=NAME, email=EMAIL))
                if op == "update_1":
                    return await repo.update(1, {"name": upd_name(i), "email": upd_email(i)})
                if op == "upsert_1":
                    return await repo.upsert(
                        BenchRow(id=1, name=upd_name(i), email=upd_email(i))
                    )
                return await repo.delete(i % DELETE_ROWS + 1)

    class RawDb(FerronitRawDb):
        async def run(self, op: str, i: int) -> object:
            if op in READ_OPS:
                async with self.pool.uow(readonly=True) as uow:
                    repo = uow["bench_rows"]
                    if op == "get_1":
                        return await repo.get(1)
                    return await repo.list(limit=100)
            async with self.pool.uow() as uow:
                repo = uow["bench_rows"]
                if op == "insert_1":
                    return await repo.save({"name": NAME, "email": EMAIL})
                if op == "update_1":
                    return await repo.update(1, {"name": upd_name(i), "email": upd_email(i)})
                return await repo.delete(i % DELETE_ROWS + 1)

    return [RawModel(), RawDb(), SACore(), SAOrm(), AsyncpgRaw()]


async def measure(variant: Variant, op: str, iters: int) -> dict[str, float]:
    """Прогнать ``iters`` запросов и вернуть пропускную способность и цену CPU.

    Returns:
        ``{"req_s": запросов/с, "cpu_us": мкс CPU процесса на запрос,
        "cores": доля одного ядра, занятая клиентом}``. CPU берётся
        ``time.process_time()`` (сумма по потокам процесса), поэтому под
        асинхронным клиентом в одном потоке это цена Python-слоя, а не ожидания.
    """
    run = variant.run
    cpu_start = time.process_time()
    start = time.perf_counter()
    for i in range(iters):
        await run(op, i)
    elapsed = time.perf_counter() - start
    cpu = time.process_time() - cpu_start
    return {
        "req_s": iters / elapsed,
        "cpu_us": cpu / iters * 1e6,
        "cores": cpu / elapsed,
    }


async def main() -> None:
    """Прогнать матрицу «операция × слой» и напечатать таблицу запросов/с."""
    import asyncpg

    variants = build_variants()
    for v in variants:
        await v.setup()

    conn = await asyncpg.connect(DSN)
    await conn.execute(CREATE)
    print(
        f"# Слои доступа к PostgreSQL, {datetime.now():%Y-%m-%d %H:%M}\n"
        f"# {platform.platform()} | ядер: {os.cpu_count()} | load: "
        f"{os.getloadavg()[0]:.2f}\n"
        f"# 1 клиент, 1 процесс, транзакция на запрос, лучшее из {RUNS}; "
        f"CPU -- time.process_time() процесса (цена Python-слоя)\n"
    )

    results: dict[str, dict[str, dict[str, float] | None]] = {op: {} for op in OPS}
    for op in OPS:
        iters = ITERS[op]
        # Снять остаточный I/O от предыдущей операции: без этого первый замер
        # после тяжёлой фазы врёт втрое (проверено: 319 против 939 запросов/с).
        await conn.execute("CHECKPOINT")
        await asyncio.sleep(PAUSE)
        for _ in range(RUNS):
            for v in variants:
                await seed(seed_rows_for(op))          # вне тайминга
                await conn.execute("CHECKPOINT")       # снять накопленный WAL
                if op not in v.ops:
                    results[op][v.name] = None
                    continue
                value = await measure(v, op, iters)
                prev = results[op].get(v.name)
                if prev is None or value["req_s"] > prev["req_s"]:
                    results[op][v.name] = value

    names = [v.name for v in variants]

    def table(title: str, fmt, get) -> None:
        """Напечатать блок «операция × слой» с заданным форматом клетки."""
        print(f"\n{title}")
        head = f"{'операция':<10}" + "".join(f"{n:>20}" for n in names)
        print(head)
        print("-" * len(head))
        for op in OPS:
            cells = []
            for n in names:
                value = results[op][n]
                cells.append(f"{'—':>20}" if value is None else f"{fmt(get(value)):>20}")
            print(f"{op:<10}" + "".join(cells))

    table(
        "запросов/с (1 клиент):",
        lambda v: f"{v:,.0f}",
        lambda r: r["req_s"],
    )
    table(
        "CPU процесса на запрос, мкс:",
        lambda v: f"{v:,.0f}",
        lambda r: r["cpu_us"],
    )
    table(
        "занято ядер (1 клиент):",
        lambda v: f"{v:.2f}",
        lambda r: r["cores"],
    )
    table(
        "потолок одного ядра, запросов/с:",
        lambda v: f"{v:,.0f}",
        lambda r: 1e6 / r["cpu_us"],
    )
    print(f"\nитераций на прогон: " + ", ".join(f"{op}={ITERS[op]}" for op in OPS))

    print("\nсоотношение (Ferronit rawmodel / слой), запросов/с и CPU на запрос:")
    for op in OPS:
        fm = results[op]["ferronit-rawmodel"]
        if not fm:
            continue
        cells = []
        for ref in ("sqlalchemy-core", "sqlalchemy-orm", "asyncpg"):
            base = results[op].get(ref)
            cells.append(
                f"{ref} " + (f"×{fm['req_s'] / base['req_s']:.2f} / ×{fm['cpu_us'] / base['cpu_us']:.2f}" if base else "—")
            )
        print(f"  {op:<10} " + "   ".join(cells))

    rows_now = await conn.fetchval("SELECT count(*) FROM bench_rows")
    row1 = await conn.fetchrow("SELECT id, name, email FROM bench_rows WHERE id = 1")
    print(f"\nконтроль: строк в bench_rows = {rows_now}, id=1 = {dict(row1) if row1 else None}")
    await conn.execute("ALTER TABLE bench_rows SET (autovacuum_enabled = true)")
    await conn.close()
    for v in variants:
        await v.close()


if __name__ == "__main__":
    asyncio.run(main())
