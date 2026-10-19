"""Вариант «потолок»: тело собрано один раз, в запросе только чтение и отдача."""

import os

os.environ["BENCH_MODE"] = "cached"

from serdes_bench._app import app  # noqa: E402  (режим задаётся до импорта)

__all__ = ["app"]
