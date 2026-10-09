# Ferrox — документация

Ferrox — высокопроизводительный Python ASGI-фреймворк с **ядром на Rust**.
Маршрутизация, разбор запроса, сериализация JSON, gzip и CORS выполняются в
нативном коде (`ferrox._core`); ваши хендлеры остаются обычным Python.

## Содержание

### Начало работы
- [Быстрый старт](getting-started.md) — установка, первое приложение, запуск, структура проекта
- [Маршрутизация и хендлеры](routing.md) — роуты, path-параметры, возвращаемые значения, ответы
- [Запросы](requests.md) — `Request`: query, заголовки, cookies, тело, JSON, формы, файлы
- [Внедрение зависимостей](injection.md) — типизированные параметры, `Header`, списки, union, body-модели

### Возможности
- [Модели и сериализация](models.md) — `msgspec`, Pydantic, dataclass, кодеки
- [OpenAPI](openapi.md) — автоматическая генерация схемы
- [WebSocket](websocket.md) — текстовые, бинарные и JSON-сообщения
- [Middleware и contrib](middleware.md) — CORS, security-заголовки, rate limiting, трассировка, health, статика

### Слой данных
- [База данных](database.md) — `ferrox.db` (Rust), SQLAlchemy-адаптер, сырой asyncpg-слой
- [Модели и query DSL](rawmodel.md) — `Model`, `Query`, `where`-DSL, репозитории

### Архитектура
- [DDD, DI и гексагональная](architecture.md) — порты/адаптеры, шины, контейнер, конфиг, CLI
- [Rust-ядро](rust-core.md) — что лежит в `ferrox._core` и зачем

### Справочно
- [Бенчмарки](benchmarks.md) — измеренные цифры и как воспроизвести

---

## Идея на одном экране

```python
from ferrox import Ferrox
from ferrox.contrib.rawdb import RawUnitOfWork, create_raw_pool
from ferrox.contrib.rawmodel import Model, select

app = Ferrox(max_body_size=10 * 1024 * 1024)

class User(Model):
    __table__ = "users"
    id: int = 0
    name: str = ""
    age: int = 0

_pool = None

@app.route("/users")
async def list_users(age: int = 18):
    async with RawUnitOfWork(_pool, readonly=True) as uow:
        repo = uow.model(User)
        users = await repo.fetch(select(User).where(User.c.age > age))
    return {"users": users}
```

- **Ноль runtime-зависимостей** в ядре — Rust-расширение лежит внутри того же
  wheel.
- **Готово к granian** — тонкий Python-слой означает, что Ferrox сохраняет
  ~⅔ от чистого Granian там, где другие фреймворки держат ~¼.
