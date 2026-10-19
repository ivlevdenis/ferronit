"""Вариант «JSON собирает PostgreSQL» (json_agg) — потолок для сериализации."""

import os

os.environ["BENCH_MODE"] = "jsonsql"

from serdes_bench._app import app  # noqa: E402  (режим задаётся до импорта)

__all__ = ["app"]
