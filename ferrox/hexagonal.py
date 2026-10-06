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

    async def info(self, msg: str, **ctx) -> None:
        """Log an informational message.

        Args:
            msg: Human-readable message text.
            **ctx: Structured key-value context attached to the record.
        """
        ...

    async def warning(self, msg: str, **ctx) -> None:
        """Log a warning-level message.

        Args:
            msg: Human-readable message text.
            **ctx: Structured key-value context attached to the record.
        """
        ...

    async def error(self, msg: str, **ctx) -> None:
        """Log an error-level message.

        Args:
            msg: Human-readable message text.
            **ctx: Structured key-value context attached to the record.
        """
        ...


class Cache(Port):
    """Key-value cache port (Redis, Memcached, in-memory)."""

    async def get(self, key: str) -> Any | None:
        """Return the cached value for a key, or None on a miss.

        Args:
            key: Cache key.

        Returns:
            The stored value, or None if the key is absent or expired.
        """
        ...

    async def set(self, key: str, value: Any, ttl: int = 300) -> None:
        """Store a value under a key with a time-to-live.

        Args:
            key: Cache key.
            value: Value to store.
            ttl: Lifetime in seconds; adapters may treat 0 as "no expiry".
        """
        ...

    async def delete(self, key: str) -> None:
        """Evict a key; a missing key is not an error."""
        ...


class MessageBus(Port):
    """External message bus port (Kafka, RabbitMQ, PubSub)."""

    async def publish(self, topic: str, message: dict, key: str | None = None) -> None:
        """Publish a message to a topic.

        Args:
            topic: Destination topic or queue name.
            message: Message payload.
            key: Optional partition or routing key.
        """
        ...

    async def subscribe(self, topic: str, handler) -> None:
        """Register a handler to consume messages from a topic.

        Args:
            topic: Source topic or queue name.
            handler: Callable invoked per message; sync or async.
        """
        ...


class EventBus(Port):
    """In-process domain event bus — publish/subscribe within the same process."""

    async def publish(self, event: object) -> None:
        """Deliver a domain event to every subscriber registered for its type."""
        ...

    def subscribe(self, event_type: type, handler) -> None:
        """Register a handler for events of a given type.

        Args:
            event_type: Event class to listen for.
            handler: Callable invoked with the event instance; sync or async.
        """
        ...


# ── In-Memory Adapters ────────────────────────────────────────────────

class PrintLogger(Logger):
    """Logger adapter that prints records to standard output."""

    async def info(self, msg, **ctx):
        """Print an INFO-level record to standard output."""
        print(f"[INFO] {msg}", ctx)

    async def warning(self, msg, **ctx):
        """Print a WARN-level record to standard output."""
        print(f"[WARN] {msg}", ctx)

    async def error(self, msg, **ctx):
        """Print an ERROR-level record to standard output."""
        print(f"[ERROR] {msg}", ctx)


class InMemoryCache(Cache):
    """Cache adapter backed by an in-process dict; values never expire."""

    def __init__(self):
        self._store: dict[str, Any] = {}

    async def get(self, key):
        """Return the stored value for a key, or None if it was never set."""
        return self._store.get(key)

    async def set(self, key, value, ttl=300):
        """Store a value under a key; ``ttl`` is accepted for parity and ignored."""
        self._store[key] = value

    async def delete(self, key):
        """Remove a key, ignoring a missing one."""
        self._store.pop(key, None)


class InMemoryEventBus(EventBus):
    """In-process EventBus adapter that dispatches to registered handlers."""

    def __init__(self):
        self._handlers: dict[type, list] = {}

    async def publish(self, event):
        """Invoke every handler subscribed to the event's type, awaiting async ones."""
        for h in self._handlers.get(type(event), []):
            result = h(event)
            if hasattr(result, "__await__"):
                await result

    def subscribe(self, event_type, handler):
        """Append a handler to the list registered for an event type."""
        self._handlers.setdefault(event_type, []).append(handler)


# ── Unit of Work ──────────────────────────────────────────────────────

class UnitOfWork(ABC):
    """Transactional boundary for aggregates."""

    __slots__ = ()

    @abstractmethod
    async def commit(self) -> None:
        """Persist all changes accumulated within the current transaction."""
        ...

    @abstractmethod
    async def rollback(self) -> None:
        """Discard all pending changes and end the transaction."""
        ...

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Commit the block on success, roll back and re-raise on any exception."""
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
        """Return the unit of work bound to this service, or None if unbound."""
        return self._uow
