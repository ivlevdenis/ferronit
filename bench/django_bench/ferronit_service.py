"""Ferronit-эквивалент стенда `django_bench` — тот же API «сервисным» слоем.

Сравнение «один сервис, три реализации» описано в `docs/vs-django.md`: этот файл —
вариант B: агрегат + `Mapper` + фреймворковый `Repository[...]`, роуты вызывают
репозиторий напрямую (без шин). Вариант C добавляет поверх этого CQRS (шины и
хендлеры) — строка `FERRONIT_APP` в `bench/api_bench.py`; вариант A (Django ORM) —
`django_bench/`.

Эндпоинты совпадают с `django_bench.urls`:

    GET    /ping              -> {"ok": true}
    GET    /rows?limit=N      -> {"rows": [...]}
    POST   /rows              -> {"id": N}
    PUT    /rows/{id}         -> {"id": N}
    DELETE /rows/{id}         -> {"ok": true}
    POST   /rows/upsert       -> {"id": N}

Запуск (нужен PostgreSQL на :5432 и таблица bench_rows):

    .venv/bin/python -m uvicorn django_bench.ferronit_service:app --port 8321 --app-dir bench
"""

from typing import Annotated

import msgspec

from ferronit import Body, Ferronit
from ferronit.contrib.rawdb import Pool, create_pool
from ferronit.contrib.rawmodel import Mapper, Model, Repository
from ferronit.ddd import AggregateRoot
from ferronit.di import Container

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
ShopPool = Annotated[Pool, "shop"]


class BenchRow(Model):
    """Строка таблицы bench_rows — та же модель, что и в django_bench."""

    __table__ = "bench_rows"
    id: int = 0
    name: str = ""
    email: str = ""


class RowBody(msgspec.Struct):
    """Тело запроса: id нужен только для upsert."""

    id: int = 0
    name: str = ""
    email: str = ""


class Row(AggregateRoot):
    """Агрегат записи: изменение имени/почты идёт через его методы."""

    def __init__(self, row_id: int, name: str, email: str) -> None:
        super().__init__()
        self.id = row_id
        self.name = name
        self.email = email

    @classmethod
    def new(cls, name: str, email: str) -> "Row":
        """Новая запись — id выдаёт база (0 = ещё не сохранён)."""
        return cls(0, name, email)

    def rename(self, name: str, email: str) -> None:
        """Изменить содержимое записи."""
        self.name = name
        self.email = email


class RowMapper(Mapper[Row, BenchRow]):
    """Перевод «агрегат ↔ модель строки» в обе стороны."""

    def to_aggregate(self, model: BenchRow) -> Row:
        return Row(model.id, model.name, model.email)

    def from_aggregate(self, aggregate: Row) -> BenchRow:
        return BenchRow(id=aggregate.id, name=aggregate.name, email=aggregate.email)


class RowsRepository(Repository[Row, BenchRow, RowMapper, ShopPool]):
    """Репозиторий агрегата Row.

    Чтение — базовый `list()`: он сам собирает агрегаты через `RowMapper`.
    Запись — тоже базовый `save()`/`delete()`; `insert`/`update` добавлены,
    потому что INSERT должен вернуть выданный базой id, а UPDATE — обновлённую
    строку (базовый `save()` возвращает переданный агрегат).
    """

    async def insert(self, name: str, email: str) -> Row:
        async with self.uow() as uow:
            saved = await uow.model(BenchRow).save(BenchRow(name=name, email=email))
        return self._mapper.to_aggregate(saved)

    async def update(self, row_id: int, name: str, email: str) -> Row | None:
        async with self.uow() as uow:
            saved = await uow.model(BenchRow).update(row_id, {"name": name, "email": email})
        return self._mapper.to_aggregate(saved) if saved is not None else None

    def to_read_model(self, aggregate: Row) -> BenchRow:
        """Проекция агрегата в модель чтения — то, что уходит в ответ."""
        return self._mapper.from_aggregate(aggregate)


container = Container()
container.factory(RowsRepository)
app = Ferronit(container=container)


@app.lifespan
async def lifespan():
    """Пул создаётся в event loop сервера — asyncpg привязан к циклу."""
    pool = await create_pool(DSN, min_size=1, max_size=4)
    container.singleton(ShopPool, pool)
    yield
    await pool.close()


@app.route("/ping")
async def ping():
    return {"ok": True}


@app.route("/rows")
async def rows(repo: RowsRepository, limit: int = 100):
    aggregates = await repo.list(limit=limit)
    return {"rows": [repo.to_read_model(a) for a in aggregates]}


@app.route("/rows", methods=["POST"])
async def insert(body: Annotated[RowBody, Body()], repo: RowsRepository):
    return {"id": (await repo.insert(body.name, body.email)).id}


@app.route("/rows/{row_id}", methods=["PUT"])
async def update(row_id: int, body: Annotated[RowBody, Body()], repo: RowsRepository):
    saved = await repo.update(row_id, body.name, body.email)
    return {"id": saved.id if saved is not None else None}


@app.route("/rows/{row_id}", methods=["DELETE"])
async def delete(row_id: int, repo: RowsRepository):
    await repo.delete(row_id)
    return {"ok": True}


@app.route("/rows/upsert", methods=["POST"])
async def upsert(body: Annotated[RowBody, Body()], repo: RowsRepository):
    return {"id": (await repo.save(Row(body.id, body.name, body.email))).id}
