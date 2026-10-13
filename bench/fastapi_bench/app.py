"""FastAPI-реализация того же API, что и `django_bench` / `ferronit_service`.

Сравнение «один сервис, несколько реализаций» — в `docs/vs-fastapi.md`: этот файл —
вариант A (идиоматичный FastAPI: pydantic-модели, `Depends`, SQLAlchemy 2.0 async).
Стендовая версия для замеров (без pydantic, тело через `request.json()`) — строка
`FASTAPI_APP` в `bench/api_bench.py`.

Эндпоинты:

    GET    /ping              -> {"ok": true}
    GET    /rows?limit=N      -> {"rows": [...]}
    POST   /rows              -> {"id": N}
    PUT    /rows/{id}         -> {"id": N}
    DELETE /rows/{id}         -> {"ok": true}
    POST   /rows/upsert       -> {"id": N}

Запуск (нужен PostgreSQL на :5432 и таблица bench_rows):

    .venv/bin/python -m uvicorn app:app --port 8322 --app-dir bench/fastapi_bench
    # схема из коробки: /docs, /openapi.json
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Integer, String, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DSN = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/postgres"


class Base(DeclarativeBase):
    """База декларативных моделей SQLAlchemy."""


class BenchRow(Base):
    """Строка таблицы bench_rows — та же схема, что и в django_bench."""

    __tablename__ = "bench_rows"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False)


class RowIn(BaseModel):
    """Тело запроса: создание и обновление."""

    name: str = Field(min_length=1)
    email: str = Field(min_length=1)


class RowUpsert(RowIn):
    """Тело upsert — тот же набор плюс id."""

    id: int = Field(ge=1)


class RowOut(BaseModel):
    """Проекция строки в ответ (валидируется pydantic-ом)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str


engine = create_async_engine(DSN, echo=False)
factory = async_sessionmaker(engine, expire_on_commit=False)


async def session_dep() -> AsyncIterator[AsyncSession]:
    """Сессия на запрос: `Depends` открывает её и закрывает после ответа."""
    async with factory() as session:
        yield session


Session = Annotated[AsyncSession, Depends(session_dep)]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Пул движка живёт в цикле событий сервера; на shutdown закрывается."""
    yield
    await engine.dispose()


app = FastAPI(title="bench", lifespan=lifespan)


@app.get("/ping")
async def ping() -> dict[str, bool]:
    return {"ok": True}


@app.get("/rows")
async def rows(session: Session, limit: Annotated[int, Query(ge=1, le=1000)] = 100) -> dict[str, list[RowOut]]:
    result = await session.execute(select(BenchRow).limit(limit))
    return {"rows": [RowOut.model_validate(row) for row in result.scalars()]}


@app.post("/rows")
async def insert(body: RowIn, session: Session) -> dict[str, int]:
    row = BenchRow(**body.model_dump())
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return {"id": row.id}


@app.put("/rows/{row_id}")
async def update(row_id: int, body: RowIn, session: Session) -> dict[str, int | None]:
    row = await session.get(BenchRow, row_id)
    if row is None:
        return {"id": None}
    row.name, row.email = body.name, body.email
    await session.commit()
    return {"id": row.id}


@app.delete("/rows/{row_id}")
async def delete(row_id: int, session: Session) -> dict[str, bool]:
    row = await session.get(BenchRow, row_id)
    if row is not None:
        await session.delete(row)
        await session.commit()
    return {"ok": True}


@app.post("/rows/upsert")
async def upsert(body: RowUpsert, session: Session) -> dict[str, int | None]:
    stmt = pg_insert(BenchRow).values(**body.model_dump())
    stmt = stmt.on_conflict_do_update(
        index_elements=[BenchRow.id],
        set_={"name": stmt.excluded.name, "email": stmt.excluded.email},
    ).returning(BenchRow.id)
    result = await session.execute(stmt)
    await session.commit()
    return {"id": result.scalars().first()}
