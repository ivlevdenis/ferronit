"""Замер list-запроса на N строк: ferronit rawmodel/rawdb против сырого asyncpg.

Отдельный стенд для случая «много строк с датами в одном ответе»: тот же
CREATE/seed/measure, что и в ``queries_vs_sqlalchemy.py``, но ``LIMIT``
настраивается и по умолчанию равен 1000, а SQLAlchemy не мерится (нужен только
слой ferronit на asyncpg и сам asyncpg как ориентир).

Смысл: ``list_100`` возвращает 100 строк x 3 поля дат/времени, а ``LIMIT=1000`` —
в 10 раз больше, поэтому цена декодирования datetime в драйвере видна отчётливее.

Окружение:
    BENCH_DSN          DSN PostgreSQL (по умолчанию :5432)
    BENCH_LIST_LIMIT   сколько строк в ответе (по умолчанию 1000)
    BENCH_ITERS        запросов на замер (по умолчанию 300)
    BENCH_RUNS         прогонов, берётся лучшее (по умолчанию 3)

Запуск: .venv/bin/python bench/list_rows_bench.py
"""

from __future__ import annotations

import asyncio
import os
import platform
from datetime import UTC, date, datetime, timedelta

from queries_vs_sqlalchemy import DSN, measure, seed

LIMIT = int(os.environ.get("BENCH_LIST_LIMIT", "1000"))
ITERS = int(os.environ.get("BENCH_ITERS", "300"))
RUNS = int(os.environ.get("BENCH_RUNS", "3"))
ROWS = max(LIMIT, 1000)


class Variant:
    """Один слой доступа; мерится только ``list`` с общим ``LIMIT``."""

    name = "?"

    async def setup(self) -> None:
        """Открыть пул (внутри event loop)."""

    async def close(self) -> None:
        """Закрыть пул."""

    async def run(self, op: str, i: int) -> object:
        """Выполнить один list-запрос."""
        raise NotImplementedError


class RawModel(Variant):
    """Ferronit: типизированные модели поверх asyncpg."""

    name = "ferronit-rawmodel"

    async def setup(self) -> None:
        from ferronit.contrib.rawdb import create_pool

        self.pool = await create_pool(DSN, min_size=1, max_size=16)

    async def close(self) -> None:
        await self.pool.close()

    async def run(self, op: str, i: int) -> object:
        async with self.pool.uow(readonly=True) as uow:
            return await uow.model(BenchRow).list(limit=LIMIT)


class RawDb(Variant):
    """Ferronit: dict-строки и явный SQL поверх asyncpg."""

    name = "ferronit-rawdb"

    async def setup(self) -> None:
        from ferronit.contrib.rawdb import create_pool

        self.pool = await create_pool(DSN, min_size=1, max_size=16)

    async def close(self) -> None:
        await self.pool.close()

    async def run(self, op: str, i: int) -> object:
        async with self.pool.uow(readonly=True) as uow:
            return await uow["bench_rows"].list(limit=LIMIT)


class AsyncpgRaw(Variant):
    """Сырой asyncpg: тот же SELECT, ориентир «дна»."""

    name = "asyncpg"

    async def setup(self) -> None:
        import asyncpg

        self.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=16)

    async def close(self) -> None:
        await self.pool.close()

    async def run(self, op: str, i: int) -> object:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT id, name, email, created_at, birthday, ttl "
                "FROM bench_rows LIMIT $1",
                LIMIT,
            )


from ferronit.contrib.rawmodel import Model  # noqa: E402


class BenchRow(Model):
    """Строка ``bench_rows`` для rawmodel-слоя."""

    __table__ = "bench_rows"
    id: int = 0
    name: str = ""
    email: str = ""
    created_at: datetime = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
    birthday: date = date(1990, 1, 15)
    ttl: timedelta = timedelta(hours=1)


async def main() -> None:
    """Прогреть слои, прогнать замер и напечатать таблицу."""
    import asyncpg

    variants = [RawModel(), RawDb(), AsyncpgRaw()]
    for v in variants:
        await v.setup()
    await seed(ROWS)

    print(
        f"# list LIMIT {LIMIT}, строк в таблице {ROWS} | asyncpg {asyncpg.__version__}\n"
        f"# {datetime.now():%Y-%m-%d %H:%M} | {platform.platform()} | "
        f"ядер: {os.cpu_count()} | load: {os.getloadavg()[0]:.2f}\n"
        f"# 1 клиент, 1 процесс, чтение readonly, лучшее из {RUNS}, итераций {ITERS}\n"
    )

    for v in variants:            # прогрев
        await v.run("list", 0)

    best: dict[str, dict[str, float]] = {}
    for _ in range(RUNS):
        for v in variants:
            value = await measure(v, "list", ITERS)
            prev = best.get(v.name)
            if prev is None or value["req_s"] > prev["req_s"]:
                best[v.name] = value

    names = [v.name for v in variants]
    head = f"{'операция':<12}" + "".join(f"{n:>20}" for n in names)
    for metric, title, fmt in (
        ("req_s", "запросов/с (1 клиент):", lambda x: f"{x:,.0f}"),
        ("cpu_us", "CPU процесса на запрос, мкс:", lambda x: f"{x:,.0f}"),
        ("cores", "занято ядер:", lambda x: f"{x:.2f}"),
        ("us_per_row", "CPU на строку ответа, мкс:", lambda x: f"{x:.3f}"),
    ):
        print(f"\n{title}")
        print(head)
        print("-" * len(head))
        if metric == "us_per_row":
            cells = "".join(f"{fmt(best[n]['cpu_us'] / LIMIT):>20}" for n in names)
        else:
            cells = "".join(f"{fmt(best[n][metric]):>20}" for n in names)
        print(f"{'list':<12}" + cells)

    conn = await asyncpg.connect(DSN)
    try:
        row = await conn.fetchrow(
            "SELECT created_at, birthday, ttl FROM bench_rows WHERE id = 1"
        )
    finally:
        await conn.close()
    print(f"\nконтроль: id=1 = {dict(row) if row else None}")
    for v in variants:
        await v.close()


if __name__ == "__main__":
    asyncio.run(main())
