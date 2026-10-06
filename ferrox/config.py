"""Configuration port — env-based settings with typed parsing."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any

__all__ = ["Config", "EnvConfig"]


class Config(ABC):
    """Configuration port — read typed settings."""

    @abstractmethod
    def get(self, key: str, default: Any = None) -> str | None:
        """Return the raw string value stored under ``key``, or ``default``."""
        ...

    @abstractmethod
    def int(self, key: str, default: int = 0) -> int:
        """Return ``key`` parsed as an integer, falling back to ``default``."""
        ...

    @abstractmethod
    def float(self, key: str, default: float = 0.0) -> float:
        """Return ``key`` parsed as a float, falling back to ``default``."""
        ...

    @abstractmethod
    def bool(self, key: str, default: bool = False) -> bool:
        """Return ``key`` parsed as a boolean, falling back to ``default``."""
        ...

    @abstractmethod
    def list(self, key: str, sep: str = ",", default: list | None = None) -> list[str]:
        """Return ``key`` split by ``sep`` into a list of stripped items."""
        ...


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
        """Read a raw environment variable.

        Args:
            key: Variable name, without the configured prefix.
            default: Value returned when the variable is not set.

        Returns:
            The raw string value, or ``default`` when unset.
        """
        return self._env.get(self._key(key), default)

    def int(self, key: str, default: int = 0) -> int:
        """Read an integer environment variable.

        Unparsable values (for example ``PORT=oops``) are swallowed and
        ``default`` is returned rather than raising.

        Args:
            key: Variable name, without the configured prefix.
            default: Value used when the variable is unset or unparsable.

        Returns:
            The parsed integer, or ``default``.
        """
        try:
            return int(self._env.get(self._key(key), str(default)))
        except (TypeError, ValueError):
            return default

    def float(self, key: str, default: float = 0.0) -> float:
        """Read a float environment variable.

        Unparsable values are swallowed and ``default`` is returned rather
        than raising.

        Args:
            key: Variable name, without the configured prefix.
            default: Value used when the variable is unset or unparsable.

        Returns:
            The parsed float, or ``default``.
        """
        try:
            return float(self._env.get(self._key(key), str(default)))
        except (TypeError, ValueError):
            return default

    def bool(self, key: str, default: bool = False) -> bool:
        """Read a boolean environment variable.

        Accepts ``1``/``true``/``yes``/``on`` as ``True`` and the empty string,
        ``0``/``false``/``no``/``off`` as ``False``; anything else yields
        ``default``.

        Args:
            key: Variable name, without the configured prefix.
            default: Value used when the variable is unset or unrecognised.

        Returns:
            The parsed boolean, or ``default``.
        """
        val = self._env.get(self._key(key), "").lower()
        if val in ("", "0", "false", "no", "off"):
            return False
        if val in ("1", "true", "yes", "on"):
            return True
        return default

    def list(self, key: str, sep: str = ",", default: list | None = None) -> list[str]:
        """Read a list environment variable split by a separator.

        Each item is stripped of surrounding whitespace and empty items are
        dropped.

        Args:
            key: Variable name, without the configured prefix.
            sep: Separator between items.
            default: Value returned when the variable is unset or empty.

        Returns:
            The parsed list of strings, or ``default`` (or an empty list).
        """
        val = self._env.get(self._key(key), "")
        if not val:
            return default or []
        return [item.strip() for item in val.split(sep) if item.strip()]
