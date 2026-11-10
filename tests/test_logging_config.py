"""Tests for the structlog/stdlib logging bridge.

The contract:
  * stdlib AND structlog loggers both render through one pipeline,
  * the bound correlation-id lands on every line (the whole point — before
    the bridge only ~5 structlog loggers saw it, never the ~100 stdlib ones),
  * json vs console is chosen by log_format,
  * configure_logging is idempotent and never removes foreign handlers
    (pytest's caplog handler must survive).
"""

from __future__ import annotations

import io
import json
import logging
from types import SimpleNamespace

import pytest
import structlog

from bifrost.logging_config import (
    _BIFROST_HANDLER_FLAG,
    _use_json,
    configure_logging,
)


def _settings(log_format: str = "json", log_level: str = "INFO") -> SimpleNamespace:
    return SimpleNamespace(log_level=log_level, log_format=log_format)


@pytest.fixture(autouse=True)
def _restore_logging():
    """Snapshot + restore global logging/structlog state around each test."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_structlog = structlog.get_config()
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    structlog.configure(**saved_structlog)


def _redirect_bifrost_handler_to_buffer() -> io.StringIO:
    """Point the handler configure_logging installed at an in-memory buffer
    so we can assert on exactly what it renders."""
    root = logging.getLogger()
    handler = next(h for h in root.handlers if getattr(h, _BIFROST_HANDLER_FLAG, False))
    buf = io.StringIO()
    handler.stream = buf  # type: ignore[attr-defined]
    return buf


def _last_json_line(text: str) -> dict:
    lines = [ln for ln in text.splitlines() if ln.strip().startswith("{")]
    assert lines, f"no JSON line in output: {text!r}"
    return json.loads(lines[-1])


# ── The bridge: both logger styles render JSON + correlation_id ──────


def test_stdlib_logger_emits_json_with_correlation_id() -> None:
    configure_logging(_settings("json"))
    buf = _redirect_bifrost_handler_to_buffer()
    structlog.contextvars.bind_contextvars(correlation_id="cid-stdlib")

    logging.getLogger("bifrost.test.stdlib").warning("hello %s", "world")

    rec = _last_json_line(buf.getvalue())
    assert rec["event"] == "hello world"
    assert rec["correlation_id"] == "cid-stdlib"
    assert rec["level"] == "warning"
    assert rec["logger"] == "bifrost.test.stdlib"
    assert "timestamp" in rec


def test_structlog_logger_emits_json_with_correlation_id() -> None:
    configure_logging(_settings("json"))
    buf = _redirect_bifrost_handler_to_buffer()
    structlog.contextvars.bind_contextvars(correlation_id="cid-struct")

    structlog.stdlib.get_logger("bifrost.test.struct").info("structured", n=7)

    rec = _last_json_line(buf.getvalue())
    assert rec["event"] == "structured"
    assert rec["correlation_id"] == "cid-struct"
    assert rec["n"] == 7
    assert rec["logger"] == "bifrost.test.struct"


def test_console_format_is_not_json() -> None:
    configure_logging(_settings("console"))
    buf = _redirect_bifrost_handler_to_buffer()

    logging.getLogger("bifrost.test.console").info("plain text line")

    out = buf.getvalue()
    assert "plain text line" in out
    # Console renderer output is not a JSON object.
    assert not out.strip().startswith("{")


# ── Format selection ─────────────────────────────────────────────────


def test_use_json_selection(monkeypatch) -> None:
    assert _use_json("json") is True
    assert _use_json("console") is False
    # auto: follows whether stderr is a TTY.
    monkeypatch.setattr("sys.stderr.isatty", lambda: True, raising=False)
    assert _use_json("auto") is False
    monkeypatch.setattr("sys.stderr.isatty", lambda: False, raising=False)
    assert _use_json("auto") is True


# ── Idempotency + foreign-handler safety ─────────────────────────────


def test_idempotent_single_handler() -> None:
    configure_logging(_settings("json"))
    configure_logging(_settings("json"))
    root = logging.getLogger()
    ours = [h for h in root.handlers if getattr(h, _BIFROST_HANDLER_FLAG, False)]
    assert len(ours) == 1  # second call replaced, not appended


def test_does_not_remove_foreign_handlers() -> None:
    """A handler we didn't install (e.g. pytest's caplog) must survive —
    this is why caplog-based tests keep working."""
    root = logging.getLogger()
    foreign = logging.StreamHandler(io.StringIO())
    root.addHandler(foreign)
    try:
        configure_logging(_settings("json"))
        assert foreign in logging.getLogger().handlers
    finally:
        root.removeHandler(foreign)


def test_applies_log_level() -> None:
    configure_logging(_settings("json", log_level="WARNING"))
    assert logging.getLogger().level == logging.WARNING
