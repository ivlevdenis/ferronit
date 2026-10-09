"""Разложение горячего пути Ferronit на компоненты (мкс/оп)."""
import asyncio
import time

from ferronit import Ferronit
from ferronit.core.app import _to_response
from ferronit.core.request import Request
from ferronit.core.response import Response
from ferronit.contrib.tracing import current_trace_id

N = 50000

v = Ferronit()

for i in range(50):
    exec(f'@v.route("/route{i}")\ndef h{i}(req): return {{"route": {i}}}')

scope = {
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


async def noop_send(msg):
    pass


async def noop_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


def bench(fn, n=N):
    for _ in range(2000):
        fn()
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    dt = time.perf_counter() - t0
    return dt / n * 1e6  # мкс/оп


def main():
    results = {}

    # 1. Rust resolve (роутинг) — то, что в cProfile не видно
    results["rust resolve"] = bench(lambda: v._app.resolve("GET", "/route0"))

    # 2. PyRequest создание (включая Rust parse_headers)
    results["Request.__init__"] = bench(lambda: Request(scope, noop_receive, None))

    # 3. headers property (кэшируется в Python, Rust PyDict возвращается 1 раз)
    req = Request(scope, noop_receive, None)
    results["req.headers (кэш)"] = bench(lambda: req.headers)

    # 4. _to_response: dict -> Response (RustResp.json + обёртка)
    results["_to_response (dict)"] = bench(lambda: _to_response({"route": 0}))

    # 5. Response._send (кодирование заголовков + 2 await send)
    resp = _to_response({"route": 0})
    loop = asyncio.new_event_loop()
    loop.run_until_complete(resp._send(noop_send))

    def bench_send():
        loop.run_until_complete(resp._send(noop_send))

    results["Response._send"] = bench(bench_send)
    loop.close()

    # 6. gzip-проверка accept-encoding
    results["gzip check"] = bench(lambda: "gzip" in req.headers.get("accept-encoding", ""))

    # 7. current_trace_id (ContextVar.get)
    results["current_trace_id"] = bench(current_trace_id)

    # 8. middleware.wrap (пустой стек) + вызов хендлера
    handler = v._app.resolve("GET", "/route0")[0]
    wrapped = v._middleware.wrap(handler)

    def call_handler():
        r = wrapped(req)
        return r

    results["handler call (обёртка)"] = bench(call_handler)

    # 9. Полный цикл через __call__ (baseline)
    async def full():
        await v(scope, noop_receive, noop_send)

    async def full_loop():
        for _ in range(2000):
            await v(scope, noop_receive, noop_send)
        t0 = time.perf_counter()
        for _ in range(N):
            await v(scope, noop_receive, noop_send)
        return (time.perf_counter() - t0) / N * 1e6

    results["FULL __call__"] = asyncio.run(full_loop())

    total = results["FULL __call__"]
    print(f"{'компонент':28s} {'мкс/оп':>9s} {'% от запроса':>13s}")
    print("-" * 55)
    for name, us in sorted(results.items(), key=lambda x: -x[1]):
        pct = us / total * 100 if name != "FULL __call__" else 100.0
        print(f"{name:28s} {us:9.2f} {pct:12.1f}%")


if __name__ == "__main__":
    main()
