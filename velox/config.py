"""Configuration port — env-based settings with typed parsing."""

from __future__ import annotations

import os
from abc import ABC
from typing import Any

__all__ = ["Config", "EnvConfig"]


class Config(ABC):
    """Configuration port — read typed settings."""

    def get(self, key: str, default: Any = None) -> str | None: ...
    def int(self, key: str, default: int = 0) -> int: ...
    def float(self, key: str, default: float = 0.0) -> float: ...
    def bool(self, key: str, default: bool = False) -> bool: ...
    def list(self, key: str, sep: str = ",", default: list | None = None) -> list[str]: ...


class EnvConfig(Config):
    """Read configuration from environment variables.

    Usage:
        config = EnvConfig()
        db = config.get("DATABASE_URL")
        port = config.int("PORT", 8000)
        debug = config.bool("DEBUG")
        hosts = config.list("ALLOWED_HOSTS")
    """

    def __init__(self, prefix: str = "", env: dict | None = None):
        self._prefix = prefix
        self._env = env or os.environ

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def get(self, key: str, default: Any = None) -> str | None:
        return self._env.get(self._key(key), default)

    def int(self, key: str, default: int = 0) -> int:
        try:
            return int(self._env.get(self._key(key), str(default)))
        except (TypeError, ValueError):
            return default

    def float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self._env.get(self._key(key), str(default)))
        except (TypeError, ValueError):
            return default

    def bool(self, key: str, default: bool = False) -> bool:
        val = self._env.get(self._key(key), "").lower()
        if val in ("", "0", "false", "no", "off"):
            return False
        if val in ("1", "true", "yes", "on"):
            return True
        return default

    def list(self, key: str, sep: str = ",", default: list | None = None) -> list[str]:
        val = self._env.get(self._key(key), "")
        if not val:
            return default or []
        return [item.strip() for item in val.split(sep) if item.strip()]
