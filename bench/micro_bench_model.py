"""Микро-бенч модельного пути: маппинг строк и кэш инстансов.

Зачем: ``rawdb.RawRepository`` отдаёт ``dict``, ``rawmodel.RawModelRepository`` —
готовый инстанс. Модель строит его через скомпилированный ``from_row``
(``object.__new__`` + прямое присваивание слотов), поэтому здесь видно, сколько
стоит каждый способ и сколько — identity-map. Всё на настоящих asyncpg ``Record``
из локального PostgreSQL, узкая (3 поля) и широкая (10 полей) выборки.

Запуск: .venv/bin/python bench/micro_bench_model.py
Если базы нет — скрипт скажет об этом и выйдет.
"""

from __future__ import annotations

import asyncio
import time

import asyncpg

from ferrox.contrib.rawdb import RawRepository
from ferrox.contrib.rawmodel import IdentityMap, Model, RawModelRepository

DSN = "postgresql://postgres:postgres@localhost:5432/postgres"
ROWS = 1000
MAP_REPEATS = 200
FETCH_REPEATS = 20

DDL_USERS = """
CREATE TABLE IF NOT EXISTS model_users (
    id serial PRIMARY KEY,
    name text NOT NULL,
    email text NOT NULL
)
"""
DDL_WIDE = """
CREATE TABLE IF NOT EXISTS model_wide (
    id serial PRIMARY KEY,
    c1 text, c2 text, c3 integer, c4 integer, c5 boolean,
    c6 double precision, c7 text, c8 text, c9 text
)
"""
SEED_USERS = (
    "INSERT INTO model_users (name, email) "
    "SELECT 'user_' || g, 'u' || g || '@example.com' FROM generate_series(1, 1000) g"
)
SEED_WIDE = (
    "INSERT INTO model_wide (c1, c2, c3, c4, c5, c6, c7, c8, c9) "
    "SELECT 'a' || g, 'b' || g, g, g * 2, g % 2 = 0, g * 1.5, "
    "'c' || g, 'd' || g, 'e' || g FROM generate_series(1, 1000) g"
)


class User(Model):
    """Узкая модель: те же три поля, что и в таблице стенда ``users``."""

    __table__ = "model_users"
    id: int = 0
    name: str = ""
    email: str = ""


class Wide(Model):
    """Широкая модель: десять полей разных типов."""

    __table__ = "model_wide"
    id: int = 0
    c1: str = ""
    c2: str = ""
    c3: int = 0
    c4: int = 0
    c5: bool = False
    c6: float = 0.0
    c7: str = ""
    c8: str = ""
    c9: str = ""


def best_of(fn, repeats: int) -> float:
    """Минимальное время прогона ``fn`` из ``repeats`` попыток (меньше шума)."""
    fn()  # прогрев
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


async def best_of_async(factory, repeats: int) -> float:
    """То же для корутины-фабрики: каждый прогон — один ``await``."""
    await factory()
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        await factory()
        best = min(best, time.perf_counter() - start)
    return best


async def seed(conn: asyncpg.Connection) -> None:
    """Создаёт таблицы и наполняет их по 1000 строк, если они пусты."""
    await conn.execute(DDL_USERS)
    await conn.execute(DDL_WIDE)
    if await conn.fetchval("SELECT count(*) FROM model_users") == 0:
        await conn.execute(SEED_USERS)
    if await conn.fetchval("SELECT count(*) FROM model_wide") == 0:
        await conn.execute(SEED_WIDE)


async def measure_mapping(conn: asyncpg.Connection, model, table: str) -> dict[str, float]:
    """µs на строку: tuple / __init__ (путь репозитория) / from_row-обёртка / dict."""
    rows = await conn.fetch(f"SELECT * FROM {table} LIMIT {ROWS}")
    n = len(rows)

    return {
        "tuple": best_of(lambda: [tuple(row) for row in rows], MAP_REPEATS) / n * 1e6,
        "init": best_of(lambda: [model(*row) for row in rows], MAP_REPEATS) / n * 1e6,
        "from_row": best_of(lambda: [model.from_row(row) for row in rows], MAP_REPEATS) / n * 1e6,
        "dict": best_of(lambda: [dict(row) for row in rows], MAP_REPEATS) / n * 1e6,
    }


async def measure_fetch(conn: asyncpg.Connection, model, table: str) -> dict[str, float]:
    """Полный путь ``SELECT + маппинг`` для dict- и модельного репозитория."""
    raw = RawRepository(conn, table)
    model_repo = RawModelRepository(conn, model)

    raw_s = await best_of_async(lambda: raw.list(limit=ROWS), FETCH_REPEATS)
    model_s = await best_of_async(lambda: model_repo.list(limit=ROWS), FETCH_REPEATS)
    return {"raw": raw_s / ROWS * 1e6, "model": model_s / ROWS * 1e6}


def measure_identity_map(model) -> dict[str, float]:
    """µs на операцию для identity-map: запись (put) и чтение (get)."""
    columns = len(model.__columns__)
    objects = [model.from_row([0] * columns) for _ in range(ROWS)]
    keys = list(range(ROWS))
    identity_map = IdentityMap()

    put = best_of(
        lambda: [identity_map.put(model, k, o) for k, o in zip(keys, objects)], MAP_REPEATS
    ) / ROWS * 1e6
    for k, o in zip(keys, objects):
        identity_map.put(model, k, o)
    get = best_of(lambda: [identity_map.get(model, k) for k in keys], MAP_REPEATS) / ROWS * 1e6
    return {"put": put, "get": get}


def print_row(
    label: str, mapping: dict[str, float], fetch: dict[str, float], imap: dict[str, float]
) -> None:
    """Печатает строку отчёта по одной модели."""
    print(
        f"{label:16s} "
        f"tuple {mapping['tuple']:5.2f} | init(repo) {mapping['init']:5.2f} | "
        f"from_row {mapping['from_row']:5.2f} | dict {mapping['dict']:5.2f} | "
        f"dict/init ×{mapping['dict'] / mapping['init']:.2f} | "
        f"SELECT+map dict {fetch['raw']:5.2f} vs model {fetch['model']:5.2f} | "
        f"imap put {imap['put']:5.3f} get {imap['get']:5.3f}"
    )


async def main() -> None:
    """Гоняет стенд на локальном PostgreSQL."""
    try:
        conn = await asyncpg.connect(DSN)
    except Exception as exc:  # pragma: no cover - зависит от окружения
        print(f"PostgreSQL недоступен ({type(exc).__name__}); подними контейнер на :5432")
        return

    try:
        await seed(conn)
        print(
            f"строк на выборку: {ROWS}, лучшее из {MAP_REPEATS} прогонов, µs на строку "
            f"(imap — µs на операцию)\n"
        )
        for label, model, table in (
            ("users (3 поля)", User, "model_users"),
            ("wide (10 полей)", Wide, "model_wide"),
        ):
            mapping = await measure_mapping(conn, model, table)
            fetch = await measure_fetch(conn, model, table)
            imap = measure_identity_map(model)
            print_row(label, mapping, fetch, imap)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
