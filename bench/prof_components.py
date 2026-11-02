"""Разложение горячего пути Ferronit по RSGI на компоненты (мкс/оп)."""
import asyncio
import time

import _rsgi
from ferronit import Ferronit
from ferronit.core.app import _rsgi_request, _rsgi_send, _to_response
from ferronit.contrib.tracing import current_trace_id

N = 50000

v = Ferronit()

for i in range(50):
    exec(f'@v.route("/route{i}")\ndef h{i}(req): return {{"route": {i}}}')

HEADERS = (("host", "test"), ("accept-encoding", "gzip"), ("user-agent", "bench"))
scope = _rsgi.Scope(method="GET", path="/route0", headers=HEADERS)
proto = _rsgi.Proto()


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

    # 2. Request создание из RSGI-scope (включая Rust parse_headers)
    results["_rsgi_request"] = bench(lambda: _rsgi_request(scope, proto, None, None))

    # 3. headers property (кэшируется в Python, Rust PyDict возвращается 1 раз)
    req = _rsgi_request(scope, proto, None, None)
    results["req.headers (кэш)"] = bench(lambda: req.headers)

    # 4. _to_response: dict -> Response (msgspec + обёртка)
    results["_to_response (dict)"] = bench(lambda: _to_response({"route": 0}))

    # 5. _rsgi_send (кодирование заголовков + один response_bytes)
    resp = _to_response({"route": 0})
    results["_rsgi_send"] = bench(lambda: _rsgi_send(proto, resp))

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

    # 9. Полный цикл через __rsgi__ (baseline)
    async def full_loop():
        for _ in range(2000):
            await v.__rsgi__(scope, proto)
        t0 = time.perf_counter()
        for _ in range(N):
            await v.__rsgi__(scope, proto)
        return (time.perf_counter() - t0) / N * 1e6

    results["FULL __rsgi__"] = asyncio.run(full_loop())

    total = results["FULL __rsgi__"]
    print(f"{'компонент':28s} {'мкс/оп':>9s} {'% от запроса':>13s}")
    print("-" * 55)
    for name, us in sorted(results.items(), key=lambda x: -x[1]):
        pct = us / total * 100 if name != "FULL __rsgi__" else 100.0
        print(f"{name:28s} {us:9.2f} {pct:12.1f}%")


if __name__ == "__main__":
    main()
