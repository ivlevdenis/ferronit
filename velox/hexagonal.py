"""Hexagonal architecture — Ports & Adapters for DDD."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

__all__ = [
    "Adapter",
    "ApplicationService",
    "Cache",
    "EventBus",
    "Logger",
    "MessageBus",
    "Port",
    "UnitOfWork",
]


# ── Base ──────────────────────────────────────────────────────────────

class Port(ABC):
    """Input/output port interface — dependency inversion boundary."""


class Adapter(Port):
    """Concrete implementation of a port (e.g. PostgresRepo, RedisBus)."""


# ── Standard Ports ────────────────────────────────────────────────────

class Logger(Port):
    """Application logger port."""

    async def info(self, msg: str, **ctx) -> None: ...
    async def warning(self, msg: str, **ctx) -> None: ...
    async def error(self, msg: str, **ctx) -> None: ...


class Cache(Port):
    """Key-value cache port (Redis, Memcached, in-memory)."""

    async def get(self, key: str) -> Any | None: ...
    async def set(self, key: str, value: Any, ttl: int = 300) -> None: ...
    async def delete(self, key: str) -> None: ...


class MessageBus(Port):
    """External message bus port (Kafka, RabbitMQ, PubSub)."""

    async def publish(self, topic: str, message: dict, key: str | None = None) -> None: ...
    async def subscribe(self, topic: str, handler) -> None: ...


class EventBus(Port):
    """In-process domain event bus — publish/subscribe within the same process."""

    async def publish(self, event: object) -> None: ...
    def subscribe(self, event_type: type, handler) -> None: ...


# ── In-Memory Adapters ────────────────────────────────────────────────

class PrintLogger(Logger):
    async def info(self, msg, **ctx): print(f"[INFO] {msg}", ctx)
    async def warning(self, msg, **ctx): print(f"[WARN] {msg}", ctx)
    async def error(self, msg, **ctx): print(f"[ERROR] {msg}", ctx)


class InMemoryCache(Cache):
    def __init__(self):
        self._store: dict[str, Any] = {}
    async def get(self, key): return self._store.get(key)
    async def set(self, key, value, ttl=300): self._store[key] = value
    async def delete(self, key): self._store.pop(key, None)


class InMemoryEventBus(EventBus):
    def __init__(self):
        self._handlers: dict[type, list] = {}
    async def publish(self, event):
        for h in self._handlers.get(type(event), []):
            result = h(event)
            if hasattr(result, "__await__"):
                await result
    def subscribe(self, event_type, handler):
        self._handlers.setdefault(event_type, []).append(handler)


# ── Unit of Work ──────────────────────────────────────────────────────

class UnitOfWork(ABC):
    """Transactional boundary for aggregates."""

    __slots__ = ()

    @abstractmethod
    async def commit(self) -> None: ...
    @abstractmethod
    async def rollback(self) -> None: ...

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        try:
            yield
            await self.commit()
        except Exception:
            await self.rollback()
            raise


# ── Application Service ───────────────────────────────────────────────

class ApplicationService:
    """Base for application services — holds port references, no domain logic."""

    __slots__ = ("_uow",)

    def __init__(self, uow: UnitOfWork | None = None) -> None:
        self._uow = uow

    @property
    def uow(self) -> UnitOfWork | None:
        return self._uow
