"""Пробник: что батчинг делает с сохранностью данных (а что — не делает).

Проверяет три вещи прямыми опытами на живом PostgreSQL:

    1. Атомарность: конфликт на 50-й строке батча — сколько строк доехало.
    2. Лимит запроса: сколько строк влезает в один multi-row INSERT.
    3. Durability: COMMIT батча при `synchronous_commit=on` -> SIGKILL контейнера
       -> старт -> строки на месте? И то же при `synchronous_commit=off` (риск).

Контейнер `velox-pg` перезапускается жёстко (kill = SIGKILL), поэтому таблица
пересоздаётся в начале. Данные стенда (`bench_rows`) не нужны никому, кроме него.

Запуск: .venv/bin/python bench/_probe_batch_durability.py
"""

from __future__ import annotations

import asyncio
import subprocess
import time

import asyncpg

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
CONTAINER = "velox-pg"
CREATE = (
    "CREATE TABLE IF NOT EXISTS bench_batch ("
    "id serial PRIMARY KEY, name text NOT NULL UNIQUE)"
)


def restart_container() -> None:
    """Жёстко убить контейнер с базой и поднять снова (имитация краха)."""
    subprocess.run(["docker", "kill", CONTAINER], check=True, capture_output=True)
    time.sleep(1)
    subprocess.run(["docker", "start", CONTAINER], check=True, capture_output=True)


async def connect(timeout: float = 60.0) -> asyncpg.Connection:
    """Дождаться готовности базы и подключиться."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return await asyncpg.connect(DSN)
        except Exception:
            await asyncio.sleep(0.5)
    raise TimeoutError("база не поднялась")


async def reset(conn) -> None:
    """Пересоздать таблицу пробника."""
    await conn.execute(CREATE)
    await conn.execute("TRUNCATE bench_batch RESTART IDENTITY")


async def test_atomicity(conn) -> None:
    """Батч с конфликтом на 50-й строке: сколько строк осталось в таблице."""
    await reset(conn)
    # Одна строка уже есть — батч упадёт на дубликате name в середине.
    await conn.execute("INSERT INTO bench_batch (name) VALUES ('dup')")
    values = ", ".join(
        [f"('user_{j}')" for j in range(49)] + ["('dup')"] + [f"('user_{j}')" for j in range(50, 100)]
    )
    try:
        await conn.execute(f"INSERT INTO bench_batch (name) VALUES {values}")
        print("атомарность: батч прошёл (неожиданно)")
    except asyncpg.UniqueViolationError as exc:
        count = await conn.fetchval("SELECT count(*) FROM bench_batch")
        print(
            f"атомарность: конфликт на 50-й строке -> строк в таблице {count} "
            f"(1 = доехала только до-батчевая строка), ошибка: {type(exc).__name__}"
        )


async def test_limits(conn) -> None:
    """Сколько строк влезает в один multi-row INSERT (лимит параметров)."""
    await reset(conn)
    for rows in (10_000, 22_000):
        values = ", ".join(f"('u{rows}_{j}')" for j in range(rows))
        try:
            await conn.execute(f"INSERT INTO bench_batch (name) VALUES {values}")
            print(f"лимит: {rows} строк одним оператором — прошло")
        except Exception as exc:  # noqa: BLE001 - печатаем любой отказ драйвера/сервера
            print(f"лимит: {rows} строк одним оператором — отказ ({type(exc).__name__}: {str(exc)[:90]})")
        await reset(conn)


async def test_durability(sync_mode: str) -> None:
    """Закоммитить батч, жёстко убить сервер, проверить строки после старта."""
    conn = await connect()
    await reset(conn)
    await conn.execute(f"SET synchronous_commit = {sync_mode}")
    values = ", ".join(f"('d{j}')" for j in range(500))
    await conn.execute(f"INSERT INTO bench_batch (name) VALUES {values}")
    committed = await conn.fetchval("SELECT count(*) FROM bench_batch")
    try:
        await conn.close()
    except Exception:
        pass

    restart_container()
    conn = await connect()
    after = await conn.fetchval("SELECT count(*) FROM bench_batch")
    await conn.close()
    print(f"durability (synchronous_commit={sync_mode}): до краха {committed}, после краха и старта {after}")


async def main() -> None:
    """Прогнать три опыта подряд."""
    conn = await connect()
    await test_atomicity(conn)
    await test_limits(conn)
    await conn.close()
    await test_durability("on")
    await test_durability("off")


if __name__ == "__main__":
    asyncio.run(main())
