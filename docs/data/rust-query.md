# Запросы в Rust — `ferrox.db`

## Зачем

Для read-heavy хендлеров, отдающих большие выборки, Python-драйвер становится
узким местом: каждая строка проходит маппинг и сериализацию под GIL. `ferrox.db`
выполняет запрос и JSON-кодирование целиком в Rust (фоновый tokio-рантайм) —
Python не трогает строки вообще. На чтении 100 строк это **×2.4–2.5** к
Ferrox+asyncpg на одном воркере.

```python
import asyncio
import ferrox

ferrox.db.connect("postgresql://user:***@host/db", 16)   # один раз на старте

@app.route("/report")
async def report():
    body = await asyncio.to_thread(
        ferrox.db.query_json,
        "SELECT id, name FROM users ORDER BY id LIMIT 1000",
        [],
    )
    return Response(body, content_type="application/json")
```

- `connect(dsn, pool_size)` — один вызов при старте.
- `query_json(sql, params)` — синхронная; снимает GIL и ждёт tokio-рантайм,
  поэтому вызывайте её через `asyncio.to_thread`.

## Что это НЕ даёт

- Запись пока медленнее Python-драйвера (tokio-postgres пересобирает prepared
  statement на каждый вызов — asyncpg кэширует). Для записи берите `rawdb`.
- Это тонкий «запрос → JSON», а не ORM: связей, миграций и валидации тут нет.
  Если они нужны — см. [SQLAlchemy](sqlalchemy.md).
