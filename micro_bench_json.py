"""Микро-бенч сериализации: RustResp.json (write_json) vs json.dumps на большом dict."""
import json
import time

from velox_core import Response as RustResp

# Большой вложенный JSON: 200 dict-ов с примитивами, строками, списками, булевыми
payload = {
    "users": [
        {
            "id": i,
            "name": f"user_{i}",
            "active": i % 2 == 0,
            "score": i * 1.5,
            "tags": [f"tag_{j}" for j in range(5)],
            "meta": {"level": i % 10, "role": "admin" if i % 3 == 0 else "user"},
        }
        for i in range(200)
    ],
    "total": 200,
    "ok": True,
}

N = 20000


def bench_rust():
    for _ in range(N):
        r = RustResp.json(payload, 200)
        bytes(r.body)


def bench_python():
    for _ in range(N):
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


for name, fn in (("Rust write_json", bench_rust), ("Python json.dumps", bench_python)):
    # прогрев
    fn()
    t0 = time.perf_counter()
    fn()
    dt = time.perf_counter() - t0
    print(f"{name:18s} {N/dt:12,.0f} ops/s  ({dt/N*1e6:.1f} мкс/оп)")
