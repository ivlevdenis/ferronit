"""Микро-бенч «сборка ответа» в Rust-ядре: сериализация JSON и gzip.

Оба шага происходят на каждый ответ и оба — в Rust:

* `Response.json` (свой writer в буфер, без промежуточного `serde_json::Value`);
* `FerronitApp.gzip_compress` — сжатие тела, если клиент прислал `Accept-Encoding: gzip`.

Тело — эквивалент ответа из 100 строк (3 поля), как в `bench_postgres.py`.
Важный контекст: `ab` по умолчанию **не** отправляет `Accept-Encoding`, поэтому в
сетевых замерах ответы шли несжатыми, а браузер и httpx просят gzip.
"""
import time

from ferronit._core import Response as RustResp
from ferronit._core import FerronitApp

PAYLOAD = {
    "users": [
        {"id": i, "name": f"user_{i}", "email": f"u{i}@example.com"} for i in range(100)
    ]
}

app = FerronitApp()
N = 20_000


def measure(fn, arg) -> float:
    """Возвращает микросекунды на операцию (со прогревом)."""
    fn(arg)
    t0 = time.perf_counter()
    for _ in range(N):
        fn(arg)
    return (time.perf_counter() - t0) / N * 1e6


def as_json(_arg) -> None:
    resp = RustResp.json(PAYLOAD, 200)
    bytes(resp.body)


def as_gzip(body: bytes) -> None:
    bytes(app.gzip_compress(body))


body = bytes(RustResp.json(PAYLOAD, 200).body)
packed = bytes(app.gzip_compress(body))

print(f"тело ответа (100 строк): {len(body):,} байт; после gzip: {len(packed):,} байт (×{len(body) / len(packed):.1f})")
print(f"JSON (Rust write_json):   {measure(as_json, None):7.1f} мкс на ответ")
print(f"gzip_compress:            {measure(as_gzip, body):7.1f} мкс на ответ")
