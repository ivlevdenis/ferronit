"""Чистый профиль Ferronit по RSGI (production-транспорт), без httpx-шума."""
import asyncio
import cProfile
import io
import pstats

from ferronit import Ferronit

from _rsgi import call

v = Ferronit()

for i in range(50):
    exec(f'@v.route("/route{i}")\ndef h{i}(req): return {{"route": {i}}}')

HEADERS = (("host", "test"), ("accept-encoding", "gzip"), ("user-agent", "bench"))


async def run():
    for _ in range(20000):
        await call(v, method="GET", path="/route0", headers=HEADERS)


pr = cProfile.Profile()
pr.enable()
asyncio.run(run())
pr.disable()

s = io.StringIO()
pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(25)
out = s.getvalue()
# только ferronit + stdlib-внутренности, без профилировщика
for line in out.splitlines():
    if "ferronit" in line or "tottime" in line or line.startswith("ncalls") or "function calls" in line or line.startswith("---"):
        print(line)
