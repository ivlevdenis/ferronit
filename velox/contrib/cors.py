"""CORS — Rust-native, one-liner."""

from __future__ import annotations

from velox.core.app import Velox

__all__ = ["cors"]


def cors(
    allow_origins: list[str] | None = None,
    allow_methods: list[str] | None = None,
    allow_headers: list[str] | None = None,
    max_age: int = 600,
):
    """Enable CORS at Rust level — no per-request Python overhead.

    Usage:
        from velox.contrib.cors import cors
        cors(allow_origins=["*"], app=app)
    """
    origins = ", ".join(allow_origins) if allow_origins else None
    methods = ", ".join(allow_methods) if allow_methods else None
    headers = ", ".join(allow_headers) if allow_headers else None
    return _CORSConfig(origins, methods, headers, str(max_age))


class _CORSConfig:
    def __init__(self, origins, methods, headers, max_age):
        self.origins = origins
        self.methods = methods
        self.headers = headers
        self.max_age = max_age

    def apply(self, app: Velox):
        app._app.set_cors(self.origins, self.methods, self.headers, self.max_age)
