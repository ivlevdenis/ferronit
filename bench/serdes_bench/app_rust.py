"""Вариант с Rust-писателем: ответ собирает ``Response.json`` из Rust-ядра."""

import os

os.environ["BENCH_MODE"] = "rust"

from serdes_bench._app import app  # noqa: E402  (режим задаётся до импорта)

__all__ = ["app"]
