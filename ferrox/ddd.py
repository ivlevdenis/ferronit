"""DDD building blocks — Commands, Queries, Bus, Events, Repository."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

__all__ = [
    "AggregateRoot",
    "Command",
    "CommandBus",
    "DomainEvent",
    "Query",
    "QueryBus",
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
        """Register the handler for a command type, replacing any previous one.

        Args:
            cmd_type: Command class to handle.
            handler: Callable invoked with the command; sync or async.
        """
        self._handlers[cmd_type] = handler

    async def dispatch(self, command: Command) -> Any:
        """Route a command to its registered handler and return the result.

        Args:
            command: Command instance; its concrete type selects the handler.

        Returns:
            Whatever the handler returns (awaited when async).

        Raises:
            _NoHandlerError: If no handler is registered for the command type.
        """
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
        """Register the handler for a query type, replacing any previous one.

        Args:
            query_type: Query class to handle.
            handler: Callable invoked with the query; sync or async.
        """
        self._handlers[query_type] = handler

    async def ask(self, query: Query) -> Any:
        """Route a query to its registered handler and return the result.

        Args:
            query: Query instance; its concrete type selects the handler.

        Returns:
            Whatever the handler returns (awaited when async).

        Raises:
            _NoHandlerError: If no handler is registered for the query type.
        """
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
        """Buffer a domain event to be published when pulled.

        Args:
            event: Event instance to append to the aggregate's queue.
        """
        self._events.append(event)

    def pull_events(self) -> list[DomainEvent]:
        """Return the buffered events and clear the aggregate's queue.

        Returns:
            The events recorded since the previous pull, in order.
        """
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
    async def get(self, id: Any) -> Any:
        """Load the aggregate with the given identity.

        Args:
            id: Aggregate identity.

        Returns:
            The aggregate, or None if it does not exist.
        """
        ...

    @abstractmethod
    async def save(self, aggregate: Any) -> None:
        """Persist an aggregate's current state (insert or update).

        Args:
            aggregate: Aggregate instance to write.
        """
        ...


# ── Errors ────────────────────────────────────────────────────────────

class _NoHandlerError(Exception):
    pass
