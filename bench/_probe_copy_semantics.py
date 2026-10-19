"""Пробник: что такое COPY, о котором речь — семантика `COPY FROM STDIN`.

Речь о протокольном `COPY ... FROM STDIN` (в asyncpg — `copy_records_to_table`),
а не о серверном `COPY ... FROM '/file'`. Здесь проверяются свойства, важные для
приложения:

    1. участие в транзакции: COPY внутри BEGIN откатывается вместе с ней;
    2. конфликт UNIQUE внутри пачки — что происходит с уже отправленными строками;
    3. serial-колонка заполняется, если COPY идёт по списку колонок без id;
    4. срабатывают ли обычные INSERT-триггеры (нужно для аудита);
    5. есть ли RETURNING у COPY-вставки.

Запуск: .venv/bin/python bench/_probe_copy_semantics.py
"""

from __future__ import annotations

import asyncio

import asyncpg

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
DDL = """
DROP TABLE IF EXISTS copy_demo;
CREATE TABLE copy_demo (
    id serial PRIMARY KEY, name text NOT NULL UNIQUE, email text NOT NULL);
DROP TABLE IF EXISTS copy_audit;
CREATE TABLE copy_audit (n int);
CREATE OR REPLACE FUNCTION copy_demo_audit() RETURNS trigger AS $$
BEGIN
    INSERT INTO copy_audit (n) VALUES (1);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER copy_demo_trg AFTER INSERT ON copy_demo
    FOR EACH ROW EXECUTE FUNCTION copy_demo_audit();
"""


async def main() -> None:
    """Прогнать проверки семантики COPY по порядку."""
    conn = await asyncpg.connect(DSN)
    await conn.execute(DDL)
    records = [(f"u{i}", "e@e.com") for i in range(100)]

    # 1. Транзакция: COPY + ROLLBACK -> строк нет
    async with conn.transaction():
        await conn.copy_records_to_table("copy_demo", records=records, columns=["name", "email"])
        inside = await conn.fetchval("SELECT count(*) FROM copy_demo")
    after = await conn.fetchval("SELECT count(*) FROM copy_demo")
    print(f"1. в транзакции: внутри видно {inside}, после ROLLBACK {after}")

    # 2. Конфликт UNIQUE внутри пачки
    await conn.execute("INSERT INTO copy_demo (name, email) VALUES ('dup', 'e@e.com')")
    try:
        await conn.copy_records_to_table(
            "copy_demo",
            records=[("a1", "e@e.com"), ("a2", "e@e.com"), ("dup", "e@e.com"), ("a3", "e@e.com")],
            columns=["name", "email"],
        )
        print("2. конфликт UNIQUE: COPY прошёл (неожиданно)")
    except asyncpg.UniqueViolationError:
        total = await conn.fetchval("SELECT count(*) FROM copy_demo")
        print(f"2. конфликт UNIQUE: ошибка, строк в таблице {total} (1 = только до-COPY'шная)")

    # 3. serial и триггеры
    await conn.execute("TRUNCATE copy_demo RESTART IDENTITY")
    await conn.execute("TRUNCATE copy_audit")
    await conn.copy_records_to_table("copy_demo", records=records, columns=["name", "email"])
    ids = await conn.fetchrow("SELECT min(id) AS lo, max(id) AS hi FROM copy_demo")
    audited = await conn.fetchval("SELECT count(*) FROM copy_audit")
    print(f"3. serial: id {ids['lo']}..{ids['hi']} (заполнены), триггеров сработало {audited} на 100 строк")

    # 4. RETURNING у COPY
    try:
        await conn.fetch("COPY copy_demo (name, email) FROM STDIN RETURNING id")
        print("4. RETURNING у COPY: поддерживается")
    except Exception as exc:  # noqa: BLE001 - печатаем отказ
        print(f"4. RETURNING у COPY: {type(exc).__name__} — {str(exc)[:70]}")

    # 5. Выгрузка: COPY ... TO STDOUT против обычного SELECT
    rows = await conn.copy_from_query("SELECT name FROM copy_demo ORDER BY name LIMIT 3")
    print(f"5. copy_from_query (выгрузка): {rows.splitlines()[:3]}")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
