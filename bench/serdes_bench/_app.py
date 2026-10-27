"""Общая часть стенда сериализации/десериализации модели (см. bench/serdes_bench/).

Один и тот же путь до данных (framework-овый ``uow.model(BenchRow).list()``) и один и
тот же маршрут ``/rows?limit=N``, различается ТОЛЬКО сборка ответа. Так HTTP-разница
между вариантами — это ровно цена сериализации/десериализации, а не другого кода.

Нужны переменные окружения: ``BENCH_DSN`` (строка подключения), ``BENCH_MODE``.

Варианты (``BENCH_MODE``):
  framework — как сейчас: хендлер возвращает ``{\"rows\": [модели]}``, ядро отдаёт
              msgspec-кодировщик + экранирование HTML;
  noescape  — то же, но экранирование отключено (цена одного ``_escape_html``);
  cached    — потолок: та же работа сделана один раз, в запросе только отдача тела.
"""

from __future__ import annotations

import os
from typing import Annotated

import msgspec

import ferronit.core.response as core_response
from ferronit import Ferronit, Response
from ferronit.contrib.rawdb import Pool, create_pool
from ferronit.contrib.rawmodel import Model
from ferronit.di import Container

DSN = os.environ["BENCH_DSN"]
MODE = os.environ.get("BENCH_MODE", "framework")
ShopPool = Annotated[Pool, "shop"]

# JSON собирает сама PostgreSQL: один текстовый столбец вместо трёх на строку.
# string_agg+row_to_json, а не json_agg: json_agg вставляет разделители ", \n "
# и тело получается на 3 байта длиннее на строку.
JSON_AGG_SQL = (
    "SELECT '{\"rows\":[' || coalesce(string_agg(row_to_json(t)::text, ','), '') || ']}' "
    "FROM (SELECT id, name, email FROM bench_rows LIMIT $1) t"
)


class BenchRow(Model):
    """Строка таблицы ``bench_rows`` — та же модель, что в остальных стендах."""

    __table__ = "bench_rows"
    id: int = 0
    name: str = ""
    email: str = ""


class Repo:
    """Репозиторий строк: фреймворковые Unit of Work и модельный репозиторий."""

    def __init__(self, pool: ShopPool) -> None:
        self._pool = pool

    async def list(self, limit: int) -> list[BenchRow]:
        """Читает ``limit`` строк таблицы."""
        async with self._pool.uow(readonly=True) as uow:
            return await uow.model(BenchRow).list(limit=limit)

    async def list_json(self, limit: int) -> bytes:
        """То же чтение, но JSON собирает PostgreSQL — Python не трогает поля.

        Это не «модельный» путь (моделей нет вовсе): замер нужен как потолок для
        любой оптимизации сериализации — сколько стоит запрос, если сериализацию
        целиком отдать базе. Тело — тот же JSON, что собирает msgspec (сверяется
        по md5 в ``run_ab.sh``).
        """
        async with self._pool.uow(readonly=True) as uow:
            payload = await uow.connection.fetchval(JSON_AGG_SQL, limit)
        return payload.encode()


CACHE: dict[int, bytes] = {}

if MODE == "noescape":
    core_response._escape_html = lambda body: body  # noqa: E731 — замер без экранирования


def make_app() -> Ferronit:
    """Собирает приложение стенда в выбранном режиме."""
    container = Container()
    app = Ferronit(container=container)
    container.factory(Repo)

    @app.lifespan
    async def lifespan():
        pool = await create_pool(DSN, min_size=1, max_size=16)
        container.singleton(ShopPool, pool)
        yield
        await pool.close()

    @app.route("/ping")
    async def ping():
        return {"ok": True}

    @app.route("/rows")
    async def rows(repo: Repo, limit: int = 100):
        if MODE == "jsonsql":
            return Response(await repo.list_json(limit), content_type="application/json")
        models = await repo.list(limit)
        if MODE == "cached":
            body = CACHE.get(limit)
            if body is None:
                body = CACHE[limit] = msgspec.json.encode({"rows": models})
            return Response(body, content_type="application/json")
        return {"rows": models}

    return app


app = make_app()
