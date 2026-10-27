"""Non-blocking structured logging — JSON or human-readable, guaranteed delivery.

A :class:`~ferronit.Ferronit` application depends on a logger via
:class:`LoggerProtocol` and receives it through ``Ferronit(logger=...)``. This
module provides:

- :class:`LoggerProtocol` — the structural interface the framework uses
  (``info`` / ``warning`` / ``error``; keyword arguments become structured
  fields).
- :class:`Logger` — the main implementation: a non-blocking pipeline (records
  are enqueued in O(1); a single background writer thread performs the I/O)
  with guaranteed delivery (``flush`` / ``shutdown`` + :mod:`atexit`), in JSON
  or human-readable form.
- :class:`LogRecord` — the structured record type (:class:`msgspec.Struct`)
  serialised as one JSON object per line.
- :class:`StdlibLogger` — a thin adapter over a standard-library logger (the
  default when no logger is injected).

Usage::

    from ferronit import Ferronit
    from ferronit.contrib.logging import Logger, LogConfig

    app = Ferronit(
        logger=Logger(LogConfig(fmt="text", filename="/var/log/app.log")),
        access_log=True,
    )
"""

from __future__ import annotations

import atexit
import json
import logging
import queue
import sys
import threading
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Literal, Protocol, TextIO

import msgspec

from ferronit.contrib.tracing import current_trace_id

__all__ = [
    "ExcInfo",
    "JsonFormatter",
    "LogConfig",
    "LogRecord",
    "Logger",
    "LoggerProtocol",
    "StdlibLogger",
    "TextFormatter",
    "setup_logging",
]

#: Exception info accepted by :meth:`LoggerProtocol.error` (mirrors ``logging``).
ExcInfo = (
    Literal[True]
    | BaseException
    | tuple[type[BaseException], BaseException, TracebackType | None]
    | None
)

#: Numeric thresholds for the standard levels (lower = more verbose).
_DEBUG = 10
_INFO = 20
_WARNING = 30
_ERROR = 40
_CRITICAL = 50

#: Accepted level names mapped to their numeric threshold.
_LEVELS = {
    "DEBUG": _DEBUG,
    "INFO": _INFO,
    "WARNING": _WARNING,
    "ERROR": _ERROR,
    "CRITICAL": _CRITICAL,
}

#: Sentinel the writer thread uses to terminate.
_SHUTDOWN = object()


class _Flush:
    """Barrier sentinel: the writer sets ``done`` once it reaches this item."""

    __slots__ = ("done",)

    def __init__(self) -> None:
        self.done = threading.Event()


class LogRecord(msgspec.Struct, omit_defaults=True):
    """A single structured log record — one JSON object when serialised.

    Args:
        ts: Timestamp of the record (UTC).
        level: Level name (``"INFO"``, ``"WARNING"``, ``"ERROR"``).
        logger: Logical logger name.
        caller: Call site as ``"module:function:line"``.
        msg: Human-readable message.
        trace_id: Current ``X-Trace-Id`` (omitted when empty).
        exc: Formatted exception traceback (omitted when empty).
        fields: Structured fields passed as keyword arguments (omitted when
            empty).
    """

    ts: datetime
    level: str
    logger: str
    caller: str
    msg: str
    trace_id: str = ""
    exc: str = ""
    fields: dict[str, Any] = msgspec.field(default_factory=dict)


class LoggerProtocol(Protocol):
    """Structural interface for the logger a :class:`~ferronit.Ferronit` uses.

    Keyword arguments become structured fields (the ``fields`` object in
    :class:`LogRecord`). Implementations must provide ``debug``, ``info``,
    ``warning``, ``error`` and ``critical``.
    """

    def debug(self, msg: str, **fields: object) -> None:
        """Log a debug record with optional structured fields."""
        ...

    def info(self, msg: str, **fields: object) -> None:
        """Log an informational record with optional structured fields."""
        ...

    def warning(self, msg: str, **fields: object) -> None:
        """Log a warning record with optional structured fields."""
        ...

    def error(self, msg: str, *, exc_info: ExcInfo = None, **fields: object) -> None:
        """Log an error record, optionally with exception information."""
        ...

    def critical(self, msg: str, **fields: object) -> None:
        """Log a critical record with optional structured fields."""
        ...


def _caller(frame: Any) -> str:
    """Build a ``module:function:line`` string from a caller frame."""
    if frame is None:
        return "?:?:0"
    module = frame.f_globals.get("__name__", "?")
    return f"{module}:{frame.f_code.co_name}:{frame.f_lineno}"


def _format_exc(exc_info: ExcInfo) -> str:
    """Format exception info into a traceback string (``""`` when absent)."""
    if exc_info is None:
        return ""
    if exc_info is True:
        etype, value, tb = sys.exc_info()
    elif isinstance(exc_info, BaseException):
        etype, value, tb = type(exc_info), exc_info, exc_info.__traceback__
    else:
        etype, value, tb = exc_info
    return "".join(traceback.format_exception(etype, value, tb))


class JsonFormatter:
    """Serialise a :class:`LogRecord` as one JSON line via msgspec's C encoder.

    Values in ``fields`` that msgspec cannot encode (``TypeError``) fall back to
    the stdlib encoder with ``default=str``.
    """

    def format(self, record: LogRecord) -> str:
        """Serialise ``record`` into a single compact JSON line."""
        try:
            return msgspec.json.encode(record).decode("utf-8")
        except TypeError:
            return json.dumps(_as_dict(record), ensure_ascii=False, default=str)


class TextFormatter:
    """Render a :class:`LogRecord` as a human-readable single line."""

    def format(self, record: LogRecord) -> str:
        """Render ``record`` as ``ts | level | logger | msg | fields``."""
        ts = record.ts.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} | {record.level:<8} | {record.logger} | {record.msg}"
        if record.trace_id:
            line += f" | trace_id={record.trace_id}"
        for key, value in record.fields.items():
            line += f" | {key}={value}"
        if record.exc:
            line += "\n" + record.exc
        return line


def _as_dict(record: LogRecord) -> dict[str, object]:
    """Materialise a record into a plain dict for the stdlib JSON fallback."""
    payload: dict[str, object] = {
        "ts": record.ts.isoformat(timespec="milliseconds"),
        "level": record.level,
        "logger": record.logger,
        "caller": record.caller,
        "msg": record.msg,
    }
    if record.trace_id:
        payload["trace_id"] = record.trace_id
    if record.exc:
        payload["exc"] = record.exc
    if record.fields:
        payload["fields"] = record.fields
    return payload


@dataclass(frozen=True)
class LogConfig:
    """Configuration for a :class:`Logger` pipeline.

    Args:
        level: Minimum level to log (any :mod:`logging` level name, e.g.
            ``"INFO"`` or ``"DEBUG"``).
        fmt: Output format — ``"json"`` (one JSON object per line) or
            ``"text"`` (human-readable single line).
        stream: Destination text stream (defaults to ``sys.stderr``); mutually
            exclusive with ``filename``.
        filename: Destination file path (opened in append mode); mutually
            exclusive with ``stream``.
        logger: Logical logger name written into each record.
    """

    level: str = "INFO"
    fmt: Literal["json", "text"] = "json"
    stream: TextIO | None = None
    filename: str | None = None
    logger: str = "ferronit"


class _WriterThread(threading.Thread):
    """Background thread that writes rendered records to the destination."""

    def __init__(
        self,
        q: queue.Queue,
        formatter: JsonFormatter | TextFormatter,
        stream: TextIO,
    ) -> None:
        super().__init__(name="ferronit-log-writer", daemon=True)
        self._queue = q
        self._formatter = formatter
        self._stream = stream

    def run(self) -> None:
        """Render and write records until the shutdown sentinel arrives."""
        while True:
            item = self._queue.get()
            if item is _SHUTDOWN:
                break
            if isinstance(item, _Flush):
                item.done.set()
                continue
            try:
                self._stream.write(self._formatter.format(item) + "\n")
                self._stream.flush()
            except Exception:
                # Best effort: a failed write drops one record, not the thread.
                pass


class StdlibLogger:
    """Adapter forwarding records to a standard-library logger.

    The default logger a :class:`~ferronit.Ferronit` uses when none is injected.
    There is no background thread — records go straight to
    ``logging.getLogger(name)`` and integrate with whatever stdlib ``logging``
    configuration is active.
    """

    def __init__(self, name: str = "ferronit") -> None:
        self._logger = logging.getLogger(name)

    def debug(self, msg: str, **fields: object) -> None:
        """Forward a debug record to the standard-library logger."""
        self._logger.debug(msg, extra=fields)

    def info(self, msg: str, **fields: object) -> None:
        """Forward an informational record to the standard-library logger."""
        self._logger.info(msg, extra=fields)

    def warning(self, msg: str, **fields: object) -> None:
        """Forward a warning record to the standard-library logger."""
        self._logger.warning(msg, extra=fields)

    def error(self, msg: str, *, exc_info: ExcInfo = None, **fields: object) -> None:
        """Forward an error record to the standard-library logger."""
        self._logger.error(msg, exc_info=exc_info, extra=fields)

    def critical(self, msg: str, **fields: object) -> None:
        """Forward a critical record to the standard-library logger."""
        self._logger.critical(msg, extra=fields)

    @property
    def logger(self) -> logging.Logger:
        """Logger: The underlying standard-library logger."""
        return self._logger


class Logger:
    """Non-blocking logger — the main :class:`LoggerProtocol` implementation.

    Builds :class:`LogRecord` objects directly (no standard-library ``logging``
    overhead such as ``findCaller``) and enqueues them in O(1); a single
    background writer thread renders and writes them. :meth:`flush` blocks until
    every record emitted so far has been written; :meth:`shutdown` drains the
    queue, stops the thread and is registered with :mod:`atexit`, so a graceful
    process exit does not lose records.

    Args:
        config: Pipeline configuration (level, format, destination, logger name).
    """

    def __init__(self, config: LogConfig) -> None:
        if config.fmt not in ("json", "text"):
            raise ValueError(
                f"Unknown log format: {config.fmt!r} (expected 'json' or 'text')"
            )
        if config.filename is not None and config.stream is not None:
            raise ValueError("'stream' and 'filename' are mutually exclusive")

        level = config.level.upper()
        if level not in _LEVELS:
            raise ValueError(
                f"Unknown log level: {config.level!r} (expected one of {sorted(_LEVELS)})"
            )

        self._config = config
        self._level = _LEVELS[level]
        self._closed = False
        self._queue: queue.Queue = queue.Queue()
        self._formatter: JsonFormatter | TextFormatter = (
            JsonFormatter() if config.fmt == "json" else TextFormatter()
        )
        self._destination, self._own_destination = self._open_destination(config)
        self._thread = _WriterThread(self._queue, self._formatter, self._destination)
        self._thread.start()
        atexit.register(self.shutdown)

    def _open_destination(self, config: LogConfig) -> tuple[TextIO, bool]:
        """Open the destination stream; the second value is whether we own it."""
        if config.filename is not None:
            return open(config.filename, "a", encoding="utf-8"), True
        return config.stream or sys.stderr, False

    def debug(self, msg: str, **fields: object) -> None:
        """Log a debug record with optional structured fields."""
        self._enqueue("DEBUG", _DEBUG, msg, None, fields, sys._getframe(1))

    def info(self, msg: str, **fields: object) -> None:
        """Log an informational record with optional structured fields."""
        self._enqueue("INFO", _INFO, msg, None, fields, sys._getframe(1))

    def warning(self, msg: str, **fields: object) -> None:
        """Log a warning record with optional structured fields."""
        self._enqueue("WARNING", _WARNING, msg, None, fields, sys._getframe(1))

    def error(self, msg: str, *, exc_info: ExcInfo = None, **fields: object) -> None:
        """Log an error record, optionally with exception information."""
        self._enqueue("ERROR", _ERROR, msg, exc_info, fields, sys._getframe(1))

    def critical(self, msg: str, **fields: object) -> None:
        """Log a critical record with optional structured fields."""
        self._enqueue("CRITICAL", _CRITICAL, msg, None, fields, sys._getframe(1))

    def _enqueue(
        self,
        level: str,
        level_num: int,
        msg: str,
        exc_info: ExcInfo,
        fields: dict[str, object],
        frame: Any,
    ) -> None:
        """Build a :class:`LogRecord` and enqueue it without performing any I/O.

        Records below the configured level are dropped before any work is done.
        """
        if self._closed or level_num < self._level:
            return
        record = LogRecord(
            ts=datetime.now(UTC),
            level=level,
            logger=self._config.logger,
            caller=_caller(frame),
            msg=msg,
            trace_id=current_trace_id(),
            exc=_format_exc(exc_info),
            fields=fields,
        )
        self._queue.put_nowait(record)

    def flush(self, timeout: float | None = None) -> None:
        """Block until every record emitted so far has been written.

        Args:
            timeout: Maximum seconds to wait; ``None`` waits indefinitely.

        Raises:
            TimeoutError: When the writer does not drain the queue in time.
        """
        if self._closed:
            return
        barrier = _Flush()
        self._queue.put(barrier)
        if not barrier.done.wait(timeout):
            raise TimeoutError("Log flush timed out")

    def shutdown(self) -> None:
        """Drain the queue, stop the writer thread, and close the destination."""
        if self._closed:
            return
        self._closed = True
        self._queue.put(_SHUTDOWN)
        self._thread.join()
        if self._own_destination:
            self._destination.close()


def setup_logging(
    *,
    level: str = "INFO",
    fmt: Literal["json", "text"] = "json",
    stream: TextIO | None = None,
    filename: str | None = None,
    logger: str = "ferronit",
) -> Logger:
    """Build a :class:`Logger` from keyword arguments.

    A shortcut for ``Logger(LogConfig(...))``. Pass the result to
    ``Ferronit(logger=...)`` to use it, and keep the reference to call
    :meth:`Logger.flush` or :meth:`Logger.shutdown`.

    Args:
        level: Minimum level to log (e.g. ``"INFO"`` or ``"DEBUG"``).
        fmt: Output format — ``"json"`` (one JSON object per line) or
            ``"text"`` (human-readable single line).
        stream: Destination text stream (defaults to ``sys.stderr``); mutually
            exclusive with ``filename``.
        filename: Destination file path (opened in append mode); mutually
            exclusive with ``stream``.
        logger: Logical logger name written into each record.

    Returns:
        Logger: The running pipeline.
    """
    return Logger(
        LogConfig(level=level, fmt=fmt, stream=stream, filename=filename, logger=logger)
    )
