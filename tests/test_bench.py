"""Benchmark: Ferronit vs FastAPI vs raw ASGI.

Важно: pytest-benchmark НЕ умеет await'ить корутины, поэтому async-клиент
оборачивается в синхронную функцию с собственным event loop, а каждая
итерация бенча гоняет пачку реальных запросов.
"""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from ferronit import Ferronit


@pytest.fixture(scope="module")
def app():
    v = Ferronit()

    @v.route("/")
    def home(req):
        return {"ok": True}

    @v.route("/reflect")
    def reflect(req):
        return {"method": req.method, "path": req.path}

    return v


def _make_runner(app, path: str, batch: int = 100):
    """Возвращает синхронную функцию: batch реальных запросов на вызов."""
    transport = ASGITransport(app=app)

    async def _run():
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            for _ in range(batch):
                await c.get(path)

    loop = asyncio.new_event_loop()

    def _bench():
        loop.run_until_complete(_run())

    return _bench


@pytest.mark.benchmark(min_rounds=50)
def test_get(app, benchmark):
    benchmark(_make_runner(app, "/"))


@pytest.mark.benchmark(min_rounds=50)
def test_reflect(app, benchmark):
    benchmark(_make_runner(app, "/reflect"))
