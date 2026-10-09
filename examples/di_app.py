"""DI-контейнер: зависимости собираются по аннотациям, а не по строкам-ключам.

Контейнер разрешает граф один раз (при появлении сервиса), поэтому в горячем пути
запроса не тратится время на рефлексию: хендлер просто использует готовый объект.

Запуск:
    .venv/bin/python -m granian --interface asgi --no-ws examples.di_app:app
Проверка:
    curl localhost:8000/greet/Денис
    curl localhost:8000/stats
"""

from __future__ import annotations

from ferronit import Ferronit
from ferronit.di import Container
from ferronit.hexagonal import Cache, InMemoryCache, Logger, PrintLogger


class Greeter:
    """Use case with two injected ports — no framework imports in its body."""

    def __init__(self, logger: Logger, cache: Cache) -> None:
        self._log = logger
        self._cache = cache
        self._served = 0

    async def greet(self, name: str) -> str:
        """Return a greeting, counting how many times the service was used."""
        cached = await self._cache.get(f"greet:{name}")
        if cached is None:
            cached = f"Привет, {name}!"
            await self._cache.set(f"greet:{name}", cached)
        self._served += 1
        await self._log.info(f"greeted {name}")
        return cached

    @property
    def served(self) -> int:
        """int: number of greetings served by this instance."""
        return self._served


# ── Composition root ───────────────────────────────────────────────────
container = Container()
container.singleton(Logger, PrintLogger())
container.singleton(Cache, InMemoryCache())
container.factory(Greeter)  # Logger и Cache подставятся по аннотациям __init__

greeter = container.get(Greeter)  # автосборка графа происходит здесь, один раз

app = Ferronit()


@app.route("/greet/{name}")
async def greet(name: str) -> dict:
    """Greeting through the injected service."""
    return {"greeting": await greeter.greet(name)}


@app.route("/stats")
def stats() -> dict:
    """Show that the container returned a singleton-scoped service."""
    return {"served": greeter.served}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)
