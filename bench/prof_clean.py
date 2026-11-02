"""Честный замер hot path по RSGI: gc выключен, медиана из 3 прогонов."""
import asyncio
import gc
import statistics
import time

from ferronit import Ferronit

from _rsgi import call

gc.disable()

N = 50000

v = Ferronit()
for i in range(50):
    exec(f'@v.route("/route{i}")\ndef h{i}(req): return {{"route": {i}}}')


def make_headers(n_headers):
    hdrs = [("host", "test"), ("accept-encoding", "gzip"), ("user-agent", "bench")]
    hdrs += [(f"x-h{i}", f"v{i}") for i in range(max(0, n_headers - 3))]
    return tuple(hdrs)


async def run(headers, n):
    for _ in range(n):
        await call(v, method="GET", path="/route0", headers=headers)


def measure(headers):
    asyncio.run(run(headers, 3000))  # прогрев
    times = []
    for _ in range(3):
        t0 = time.perf_counter()
        asyncio.run(run(headers, N))
        times.append((time.perf_counter() - t0) / N * 1e6)
    return statistics.median(times)


for n_h in (3, 10, 20):
    us = measure(make_headers(n_h))
    print(f"FULL __rsgi__ ({n_h:2d} headers): {us:6.2f} мкс  →  {1e6/us:8,.0f} req/s")
