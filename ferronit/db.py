"""Слой данных: PostgreSQL → JSON целиком в Rust.

Обёртка над вложенным модулем ядра `ferronit._core.db`. Единое публичное имя — `ferronit.db`:

    import ferronit
    ferronit.db.connect("postgresql://user@host/db", 16)   # один раз на старте
    body = await asyncio.to_thread(ferronit.db.query_json, "SELECT id, name FROM t", [])

`query_json` — синхронная функция, снимает GIL и ждёт результат из фонового
tokio-рантайма, поэтому из asyncio-кода зови её через `asyncio.to_thread`.
"""

from ferronit._core import db as _db

connect = _db.connect
query_json = _db.query_json

__all__ = ["connect", "query_json"]
