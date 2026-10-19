"""Вариант без экранирования: та же сборка ответа, но ``_escape_html`` выключен."""

import os

os.environ["BENCH_MODE"] = "noescape"

from serdes_bench._app import app  # noqa: E402  (режим задаётся до импорта)

__all__ = ["app"]
