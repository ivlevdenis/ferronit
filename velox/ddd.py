"""DDD building blocks — Commands, Queries, Bus, Events, Repository."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, ClassVar

__all__ = [
    "Command",
    "Query",
    "CommandBus",
    "QueryBus",
    "AggregateRoot",
    "DomainEvent",
    "Repository",
]


# ── Messages ──────────────────────────────────────────────────────────

class Command:
    """Marker for command objects (write operations)."""
    __slots__ = ()


class Query:
    """Marker for query objects (read operations)."""
    __slots__ = ()


# ── Bus ───────────────────────────────────────────────────────────────

class CommandBus:
    """In-process command bus — dispatches commands to registered handlers."""

    __slots__ = ("_handlers",)

    def __init__(self) -> None:
        self._handlers: dict[type[Command], Callable] = {}

    def register(self, cmd_type: type[Command], handler: Callable) -> None:
        self._handlers[cmd_type] = handler

    async def dispatch(self, command: Command) -> Any:
        handler = self._handlers.get(type(command))
        if handler is None:
            raise _NoHandlerError(f"No handler for {type(command).__name__}")
        result = handler(command)
        if hasattr(result, "__await__"):
            return await result
        return result


class QueryBus:
    """In-process query bus — dispatches queries to registered handlers."""

    __slots__ = ("_handlers",)

    def __init__(self) -> None:
        self._handlers: dict[type[Query], Callable] = {}

    def register(self, query_type: type[Query], handler: Callable) -> None:
        self._handlers[query_type] = handler

    async def ask(self, query: Query) -> Any:
        handler = self._handlers.get(type(query))
        if handler is None:
            raise _NoHandlerError(f"No handler for {type(query).__name__}")
        result = handler(query)
        if hasattr(result, "__await__"):
            return await result
        return result


# ── Aggregate ─────────────────────────────────────────────────────────

class AggregateRoot:
    """Base for aggregates — tracks domain events."""

    __slots__ = ("_events",)

    def __init__(self) -> None:
        self._events: list[DomainEvent] = []

    def record(self, event: DomainEvent) -> None:
        self._events.append(event)

    def pull_events(self) -> list[DomainEvent]:
        events = self._events
        self._events = []
        return events


# ── Domain Event ──────────────────────────────────────────────────────

class DomainEvent:
    """Marker for domain events."""
    __slots__ = ()


# ── Repository ────────────────────────────────────────────────────────

class Repository(ABC):
    """Generic repository interface for an aggregate."""

    __slots__ = ()

    @abstractmethod
    async def get(self, id: Any) -> Any: ...

    @abstractmethod
    async def save(self, aggregate: Any) -> None: ...


# ── Errors ────────────────────────────────────────────────────────────

class _NoHandlerError(Exception):
    pass
