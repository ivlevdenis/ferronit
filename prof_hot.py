"""Чистый профиль Velox: прямой вызов ASGI, без httpx-шума."""
import asyncio
import cProfile
import io
import pstats

from velox import Velox

v = Velox()

for i in range(50):
    exec(f'@v.route("/route{i}")\ndef h{i}(req): return {{"route": {i}}}')


def make_scope():
    return {
        "type": "http",
        "method": "GET",
        "path": "/route0",
        "query_string": b"",
        "headers": [(b"host", b"test"), (b"accept-encoding", b"gzip"), (b"user-agent", b"bench")],
        "scheme": "http",
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
        "route_params": {},
    }


async def run():
    for _ in range(20000):
        sent = []
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(msg):
            sent.append(msg)
        await v(make_scope(), receive, send)


pr = cProfile.Profile()
pr.enable()
asyncio.run(run())
pr.disable()

s = io.StringIO()
pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(25)
out = s.getvalue()
# только velox + stdlib-внутренности, без профилировщика
for line in out.splitlines():
    if "velox" in line or "tottime" in line or line.startswith("ncalls") or "function calls" in line or line.startswith("---"):
        print(line)
