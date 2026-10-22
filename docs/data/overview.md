# Слой данных — rawdb + rawmodel

Ferronit использует **один** способ работы с БД: тонкий слой `rawdb` поверх драйвера
(`asyncpg` для PostgreSQL, `aiomysql` для MySQL, `aiosqlite` для SQLite) плюс типизированные
модели и query DSL `rawmodel`. Никакого ORM — явный SQL, bound-параметры, максимум throughput.
Движок выбирается схемой DSN в `create_pool`, SQL собирается деревом выражений и компилируется
под диалект соединения.

Драйверы — extras: `ferronit[postgres]` (asyncpg), `ferronit[mysql]` (aiomysql),
`ferronit[sqlite]` (aiosqlite). Модули слоя импортируются и без них — драйвер
загружается при создании пула, а его отсутствие даёт `ModuleNotFoundError`
с подсказкой, какой extra поставить.

| Модуль | Что даёт |
|---|---|
| `ferronit.contrib.sql` | дерево выражений и операторы (`Compare`, `Func`, `Select`…), диалекты (`POSTGRES`/`SQLITE`/`MYSQL`), компиляция |
| `ferronit.contrib.drivers` | адаптеры соединений и пулов (`asyncpg`, `aiomysql`, `aiosqlite`), `open_pool` по DSN |
| `ferronit.contrib.rawdb` | `Pool`, `create_pool`, `RawUnitOfWork`, `RawRepository`, `Condition` — сырые dict-строки |
| `ferronit.contrib.rawmodel` | `Model` (неизменяемый msgspec.Struct), `Query`/`select`, `Relation` (связи), `RawModelRepository`, `fetch_rows` (агрегаты) |

Различия движков спрятаны в диалекте: плейсхолдеры (`$1` / `?` / `%s`), `ANY` против `IN (…)`,
`RETURNING` (у MySQL его нет — ключ берётся у драйвера), `ON CONFLICT` против
`ON DUPLICATE KEY UPDATE`, `ILIKE` против `LIKE`.

Правило большого пальца: `pool.uow(readonly=True)` для чтений (пропускает `BEGIN`,
экономит round-trip), `uow.model(User)` для типизированного доступа к строкам.

```python
from ferronit.contrib.rawdb import create_pool
from ferronit.contrib.rawmodel import Model, select

class User(Model):
    __table__ = "users"        # по умолчанию — плюрализованное имя класса
    id: int = 0
    name: str = ""
    age: int = 0

pool = await create_pool("postgresql://user@host/db", min_size=1, max_size=16)
# Другие движки — та же строка кода: "mysql://user:pass@host/db" (extra mysql),
# "sqlite://:memory:" или "sqlite:///path/app.db" (extra sqlite).

async with pool.uow(readonly=True) as uow:
    adults = await uow.model(User).fetch(select(User).where(User.c.age >= 18).limit(50))
```

Подробный разбор API — в [rawdb и rawmodel](rawdb.md).
