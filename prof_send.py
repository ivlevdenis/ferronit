"""Микро-разбор Response._send: что именно стоит 3.86 мкс."""
import asyncio
import time

from velox.core.response import Response

N = 100000


async def noop_send(msg):
    pass


def main():
    resp = Response(body=b'{"route": 0}', status=200, content_type="application/json")

    # Прогрев
    loop = asyncio.new_event_loop()
    for _ in range(5000):
        loop.run_until_complete(resp._send(noop_send))

    # 1. Полный _send
    t0 = time.perf_counter()
    for _ in range(N):
        loop.run_until_complete(resp._send(noop_send))
    full = (time.perf_counter() - t0) / N * 1e6

    # 2. Только сборка raw_headers (без send)
    def build_headers():
        rh = [(b"content-type", b"application/json")]
        for k, v in resp._headers.items():
            rh.append((k.encode("latin-1"), v.encode("latin-1")))
        return rh

    for _ in range(5000):
        build_headers()
    t0 = time.perf_counter()
    for _ in range(N):
        build_headers()
    build = (time.perf_counter() - t0) / N * 1e6

    # 3. Только 2 await send с готовыми заголовками
    raw = [(b"content-type", b"application/json")]

    async def two_sends():
        await noop_send({"type": "http.response.start", "status": 200, "headers": raw})
        await noop_send({"type": "http.response.body", "body": b'{"route": 0}'})

    for _ in range(5000):
        loop.run_until_complete(two_sends())
    t0 = time.perf_counter()
    for _ in range(N):
        loop.run_until_complete(two_sends())
    sends = (time.perf_counter() - t0) / N * 1e6

    # 4. Один await send (стоимость одного send)
    async def one_send():
        await noop_send({"type": "http.response.body", "body": b"x"})

    for _ in range(5000):
        loop.run_until_complete(one_send())
    t0 = time.perf_counter()
    for _ in range(N):
        loop.run_until_complete(one_send())
    one = (time.perf_counter() - t0) / N * 1e6

    loop.close()

    print(f"полный _send:            {full:6.2f} мкс")
    print(f"  сборка raw_headers:    {build:6.2f} мкс")
    print(f"  2x await send:         {sends:6.2f} мкс")
    print(f"  1x await send:         {one:6.2f} мкс")
    print(f"  разница (код _send):   {full - build - sends:6.2f} мкс")


main()
