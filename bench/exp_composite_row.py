"""Можно ли отдать заполнение модели драйверу: композитный кодек asyncpg (tuple-формат).

Проверка идеи «промежуточный класс между Row/Record и msgspec.Struct»: если asyncpg
отдаёт не Record, а Python-кортеж (композитный тип из tuple-кодека), то маппинг в модель
и сборка строки могут быть дешевле. Мерим: fetch обычных колонок против fetch композита,
и оба с маппингом в модель, на 100/1000 строках.
"""

import asyncio
import sys
import time

import asyncpg
import msgspec

sys.path.insert(0, "bench")
from micro_bench_model_serdes import DSN  # noqa: E402


class BenchRow(msgspec.Struct):
    """Модель строки: то же, что rawmodel.Model в стенде."""

    id: int = 0
    name: str = ""
    email: str = ""


def best(fn, reps=200) -> float:
    """Минимум wall-времени из ``reps`` прогонов, µs."""
    fn()
    b = float("inf")
    for _ in range(reps):
        t = time.perf_counter()
        fn()
        b = min(b, time.perf_counter() - t)
    return b * 1e6


async def best_async(factory, reps=100) -> float:
    """То же для корутины-фабрики (каждый прогон — один await)."""
    await factory()
    b = float("inf")
    for _ in range(reps):
        t = time.perf_counter()
        await factory()
        b = min(b, time.perf_counter() - t)
    return b * 1e6


async def main() -> None:
    """Гоняет замер на локальном PostgreSQL."""
    conn = await asyncpg.connect(DSN)
    await conn.execute("DROP TYPE IF EXISTS exp_row CASCADE")
    await conn.execute("CREATE TYPE exp_row AS (id int, name text, email text)")
    # Композитные типы в asyncpg принимают только tuple-формат: декодер получает
    # готовый Python-кортеж значений (не Record).
    await conn.set_type_codec(
        "exp_row", schema="public", format="tuple", encoder=lambda v: v, decoder=lambda v: v
    )
    recs = await conn.fetch("SELECT id, name, email FROM bench_rows LIMIT 2")
    comps = await conn.fetch("SELECT ROW(id, name, email)::exp_row AS r FROM bench_rows LIMIT 2")
    print("тип строки:", type(recs[0]).__name__, "| тип композита:", type(comps[0]["r"]).__name__)
    print("значение композита:", comps[0]["r"], "-> модель:", BenchRow(*comps[0]["r"]))

    for n in (100, 1000):
        sql_plain = f"SELECT id, name, email FROM bench_rows LIMIT {n}"
        sql_comp = f"SELECT ROW(id, name, email)::exp_row AS r FROM bench_rows LIMIT {n}"
        recs = await conn.fetch(sql_plain)
        comps = await conn.fetch(sql_comp)
        t_fetch_rec = await best_async(lambda: conn.fetch(sql_plain))
        t_fetch_comp = await best_async(lambda: conn.fetch(sql_comp))
        t_map_rec = best(lambda: [BenchRow(*r) for r in recs])
        t_map_comp = best(lambda: [BenchRow(*v["r"]) for v in comps])
        t_full_rec = await best_async(lambda: conn.fetch(sql_plain))
        print(f"\nlimit={n} (µs на запрос):")
        print(f"  fetch обычных колонок          {t_fetch_rec:8.1f}")
        print(f"  fetch композита (tuple-кодек)  {t_fetch_comp:8.1f}")
        print(f"  Record → модель                {t_map_rec:8.1f}  (модель целиком {t_full_rec + t_map_rec:8.1f})")
        print(f"  композит → модель              {t_map_comp:8.1f}")
    await conn.execute("DROP TYPE IF EXISTS exp_row CASCADE")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
