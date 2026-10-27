"""Non-blocking JSON/text logging pipeline — formatters, delivery, injection."""

import io
import json
import logging
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from ferronit import Ferronit
from ferronit.contrib.logging import (
    JsonFormatter,
    LogConfig,
    Logger,
    LogRecord,
    StdlibLogger,
    TextFormatter,
    setup_logging,
)


def _record(msg="hello", level="INFO", *, trace_id="", exc="", fields=None) -> LogRecord:
    return LogRecord(
        ts=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        level=level,
        logger="ferronit.test",
        caller="test:_record:1",
        msg=msg,
        trace_id=trace_id,
        exc=exc,
        fields=fields or {},
    )


def test_json_formatter_fields():
    formatter = JsonFormatter()
    payload = json.loads(formatter.format(_record("hello")))
    assert payload["level"] == "INFO"
    assert payload["msg"] == "hello"
    assert payload["logger"] == "ferronit.test"
    assert payload["caller"] == "test:_record:1"
    assert payload["ts"] == "2026-01-15T12:00:00Z"
    assert "exc" not in payload
    assert "fields" not in payload  # empty fields omitted


def test_json_formatter_structured_fields():
    formatter = JsonFormatter()
    payload = json.loads(formatter.format(_record("x", fields={"user_id": 42, "path": "/a"})))
    assert payload["fields"]["user_id"] == 42
    assert payload["fields"]["path"] == "/a"


def test_json_formatter_exception():
    formatter = JsonFormatter()
    payload = json.loads(formatter.format(_record("x", exc="ValueError: boom")))
    assert "ValueError" in payload["exc"]
    assert "boom" in payload["exc"]


def test_json_formatter_msgspec_native_types():
    import datetime as dt

    formatter = JsonFormatter()
    line = formatter.format(_record("x", fields={"when": dt.datetime(2026, 1, 15, 12, 0, 0)}))
    payload = json.loads(line)
    assert payload["fields"]["when"] == "2026-01-15T12:00:00"  # msgspec native ISO-8601


def test_json_formatter_fallback_for_unserializable():
    class Thing:
        def __str__(self):
            return "thing!"

    formatter = JsonFormatter()
    line = formatter.format(_record("x", fields={"v": Thing()}))
    payload = json.loads(line)
    assert payload["fields"]["v"] == "thing!"  # stdlib fallback default=str


def test_text_formatter():
    formatter = TextFormatter()
    line = formatter.format(_record("hello"))
    assert "hello" in line
    assert "INFO" in line
    assert "ferronit.test" in line


def test_logger_delivery_and_flush():
    stream = io.StringIO()
    logger = Logger(LogConfig(stream=stream, logger="ferronit.test.delivery"))
    try:
        logger.info("ping")
        logger.flush()
        out = stream.getvalue().strip()
        assert "ping" in out
        json.loads(out)  # valid single JSON line
    finally:
        logger.shutdown()


def test_logger_structured_fields():
    stream = io.StringIO()
    logger = Logger(LogConfig(stream=stream, logger="ferronit.test.fields"))
    try:
        logger.info("order created", order_id=42, user="alice")
        logger.flush()
        payload = json.loads(stream.getvalue().strip())
        assert payload["msg"] == "order created"
        assert payload["fields"]["order_id"] == 42
        assert payload["fields"]["user"] == "alice"
    finally:
        logger.shutdown()


def test_level_filtering():
    stream = io.StringIO()
    logger = Logger(LogConfig(stream=stream, level="WARNING", logger="ferronit.test.level"))
    try:
        logger.debug("debug-msg")
        logger.info("info-msg")
        logger.warning("warn-msg")
        logger.flush()
        out = stream.getvalue()
        assert "debug-msg" not in out
        assert "info-msg" not in out
        assert "warn-msg" in out
    finally:
        logger.shutdown()


def test_all_levels_recorded():
    stream = io.StringIO()
    logger = Logger(LogConfig(stream=stream, level="DEBUG", logger="ferronit.test.levels"))
    try:
        logger.debug("d")
        logger.info("i")
        logger.warning("w")
        logger.error("e")
        logger.critical("c")
        logger.flush()
        levels = [json.loads(line)["level"] for line in stream.getvalue().splitlines()]
        assert levels == ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    finally:
        logger.shutdown()


def test_invalid_level_raises():
    with pytest.raises(ValueError):
        Logger(LogConfig(level="NOPE", stream=io.StringIO(), logger="ferronit.test.bad"))


def test_logger_caller_is_real_callsite():
    stream = io.StringIO()
    logger = Logger(LogConfig(stream=stream, logger="ferronit.test.caller"))
    try:
        logger.info("hi")
        logger.flush()
        payload = json.loads(stream.getvalue().strip())
        module, func, _ = payload["caller"].split(":")
        assert module == "test_logging"  # real call site, not stdlib
        assert func == "test_logger_caller_is_real_callsite"
    finally:
        logger.shutdown()


def test_logger_delivery_guarantee_on_shutdown():
    stream = io.StringIO()
    logger = Logger(LogConfig(stream=stream, logger="ferronit.test.shutdown"))
    logger.info("first")
    logger.info("second")
    logger.shutdown()  # drains the queue before stopping
    out = stream.getvalue()
    assert "first" in out
    assert "second" in out


def test_logger_text_format():
    stream = io.StringIO()
    logger = Logger(LogConfig(stream=stream, fmt="text", logger="ferronit.test.text"))
    try:
        logger.info("hello-text")
        logger.flush()
        assert "hello-text" in stream.getvalue()
    finally:
        logger.shutdown()


def test_setup_logging_returns_logger():
    stream = io.StringIO()
    logger = setup_logging(stream=stream, logger="ferronit.test.setup")
    try:
        assert isinstance(logger, Logger)
        logger.info("x")
        logger.flush()
        assert "x" in stream.getvalue()
    finally:
        logger.shutdown()


def test_stdlib_logger_forwards_to_logging(caplog):
    with caplog.at_level(logging.INFO, logger="ferronit.test.std"):
        StdlibLogger("ferronit.test.std").info("hello", a=1)
    assert "hello" in caplog.text


@pytest.mark.asyncio
async def test_access_log_json():
    stream = io.StringIO()
    logger = Logger(LogConfig(stream=stream, logger="ferronit.test.access"))
    app = Ferronit(logger=logger, access_log=True)

    @app.route("/ping")
    def ping(req):
        return "pong"

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/ping")
        assert r.status_code == 200
        logger.flush()
        payload = json.loads(stream.getvalue().strip())
        assert payload["fields"]["event"] == "access"
        assert payload["fields"]["method"] == "GET"
        assert payload["fields"]["path"] == "/ping"
        assert payload["fields"]["status"] == 200
        assert "duration_ms" in payload["fields"]
    finally:
        logger.shutdown()


@pytest.mark.asyncio
async def test_access_log_off_by_default():
    stream = io.StringIO()
    logger = Logger(LogConfig(stream=stream, logger="ferronit.test.off"))
    app = Ferronit(logger=logger)  # access_log defaults to False

    @app.route("/ping")
    def ping(req):
        return "pong"

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/ping")
        assert r.status_code == 200
        logger.flush()
        assert stream.getvalue() == ""  # no access log without the flag
    finally:
        logger.shutdown()


@pytest.mark.asyncio
async def test_error_logged_via_injected_logger():
    stream = io.StringIO()
    logger = Logger(LogConfig(stream=stream, logger="ferronit.test.err"))
    app = Ferronit(logger=logger)  # debug=False → 500 with a logged traceback

    @app.route("/boom")
    async def boom(req):
        raise RuntimeError("kaboom")

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/boom")
        assert r.status_code == 500
        logger.flush()
        payload = json.loads(stream.getvalue().strip())
        assert payload["fields"]["event"] == "error"
        assert "kaboom" in payload["exc"]
        assert "RuntimeError" in payload["exc"]
    finally:
        logger.shutdown()
