"""Вариант «как сейчас»: ядро отдаёт msgspec-кодировщик + экранирование HTML."""

import os

os.environ["BENCH_MODE"] = "framework"

from serdes_bench._app import app  # noqa: E402  (режим задаётся до импорта)

__all__ = ["app"]
