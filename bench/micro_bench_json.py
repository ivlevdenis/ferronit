"""Микро-бенч сериализации: RustResp.json (write_json) против json.dumps.

Три размера ответа, потому что разница меняет знак: на мелких/средних объектах
Rust быстрее (один проход по C-данным), на крупных вложенных структурах Python
выигрывает — каждый элемент пересекает границу PyO3, и это дороже, чем сам dumps.
"""
import json
import time

from ferrox._core import Response as RustResp

N = 20000


def make_payload(n_objects: int) -> dict:
    """Полезная нагрузка из `n_objects` вложенных объектов (0 — плоский мелкий ответ)."""
    if n_objects == 0:
        return {"ok": True, "n": 42, "name": "ferrox"}
    return {
        "users": [
            {
                "id": i,
                "name": f"user_{i}",
                "active": i % 2 == 0,
                "score": i * 1.5,
                "tags": [f"tag_{j}" for j in range(5)],
                "meta": {"level": i % 10, "role": "admin" if i % 3 == 0 else "user"},
            }
            for i in range(n_objects)
        ],
        "total": n_objects,
        "ok": True,
    }


def bench_rust(payload):
    for _ in range(N):
        r = RustResp.json(payload, 200)
        bytes(r.body)


def bench_python(payload):
    for _ in range(N):
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def measure(fn, payload) -> float:
    fn(payload)  # прогрев
    t0 = time.perf_counter()
    fn(payload)
    return N / (time.perf_counter() - t0)


for label, n_objects in (
    ("tiny (плоский dict)", 0),
    ("small (1 объект)", 1),
    ("medium (50 объектов)", 50),
    ("big (200 объектов)", 200),
):
    payload = make_payload(n_objects)
    rust = measure(bench_rust, payload)
    python = measure(bench_python, payload)
    ratio = rust / python
    verdict = "Rust быстрее" if ratio > 1 else "Python быстрее"
    print(f"{label:20s} Rust {rust:10,.0f} ops/s | Python {python:10,.0f} ops/s | ×{ratio:.2f} ({verdict})")
