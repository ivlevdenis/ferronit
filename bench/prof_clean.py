"""Честный замер hot path: gc выключен, медиана из 3 прогонов."""
import asyncio
import gc
import statistics
import time

from ferronit import Ferronit

gc.disable()

N = 50000

v = Ferronit()
for i in range(50):
    exec(f'@v.route("/route{i}")\ndef h{i}(req): return {{"route": {i}}}')


def make_scope(n_headers):
    hdrs = [(b"host", b"test"), (b"accept-encoding", b"gzip"), (b"user-agent", b"bench")]
    hdrs += [(f"x-h{i}".encode(), f"v{i}".encode()) for i in range(max(0, n_headers - 3))]
    return {
        "type": "http", "method": "GET", "path": "/route0",
        "query_string": b"", "headers": hdrs, "route_params": {},
    }


async def recv():
    return {"type": "http.request", "body": b"", "more_body": False}


async def send(m):
    pass


async def run(scope, n):
    for _ in range(n):
        await v(scope, recv, send)


def measure(scope):
    asyncio.run(run(scope, 3000))  # прогрев
    times = []
    for _ in range(3):
        t0 = time.perf_counter()
        asyncio.run(run(scope, N))
        times.append((time.perf_counter() - t0) / N * 1e6)
    return statistics.median(times)


for n_h in (3, 10, 20):
    us = measure(make_scope(n_h))
    print(f"FULL __call__ ({n_h:2d} headers): {us:6.2f} мкс  →  {1e6/us:8,.0f} req/s")
