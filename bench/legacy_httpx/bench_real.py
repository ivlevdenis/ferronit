"""Честный async-бенчмарк Ferronit (и FastAPI, если установлен)."""
import asyncio
import time

import httpx
from httpx import ASGITransport, AsyncClient

from ferronit import Ferronit


def make_ferronit():
    v = Ferronit()

    @v.route("/")
    def home(req):
        return {"ok": True}

    @v.route("/reflect")
    def reflect(req):
        return {"method": req.method, "path": req.path}

    return v


async def bench(client: AsyncClient, path: str, n: int = 5000) -> float:
    # прогрев
    for _ in range(200):
        await client.get(path)
    t0 = time.perf_counter()
    for _ in range(n):
        await client.get(path)
    dt = time.perf_counter() - t0
    return n / dt


async def main():
    v = make_ferronit()

    async with AsyncClient(transport=ASGITransport(app=v), base_url="http://test") as c:
        for path in ("/", "/reflect"):
            rps = await bench(c, path)
            print(f"Ferronit  {path:10s} {rps:10,.0f} req/s")

    # FastAPI для сравнения (если установлен)
    try:
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse

        fa = FastAPI()

        @fa.get("/")
        async def home():
            return {"ok": True}

        @fa.get("/reflect")
        async def reflect(request):
            return {"method": request.method, "path": request.url.path}

        async with AsyncClient(transport=ASGITransport(app=fa), base_url="http://test") as c:
            for path in ("/", "/reflect"):
                rps = await bench(c, path)
                print(f"FastAPI {path:10s} {rps:10,.0f} req/s")
    except ImportError:
        print("FastAPI не установлен — пропускаю сравнение")


if __name__ == "__main__":
    asyncio.run(main())
