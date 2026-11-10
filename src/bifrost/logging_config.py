"""Central logging configuration.

Before this, ``api/app.py`` called ``logging.basicConfig(...)`` with a
plain-text format at import time. structlog was imported in a handful of
modules but **never configured**, so:

  * the "structured logs" claim was false — everything rendered as plain text;
  * the correlation-id (bound into ``structlog.contextvars`` by
    ``CorrelationIDMiddleware``) reached only the ~5 structlog loggers, never
    the ~100 stdlib ``logging.getLogger(__name__)`` callers.

``configure_logging()`` fixes both with the canonical structlog **stdlib
bridge**: a single ``ProcessorFormatter`` renders *both* native-structlog
records and foreign stdlib records through one shared processor chain. Because
that chain starts with ``merge_contextvars``, the correlation-id (and any
other bound context) lands on **every** log line regardless of which logging
API the caller used.

Format is chosen by ``BifrostSettings.log_format``:
  * ``json``    — one JSON object per line (prod / log shippers).
  * ``console`` — colorized human-readable (local dev).
  * ``auto``    — console when stderr is a TTY, else json (the default).
"""

from __future__ import annotations

import logging
import sys

import structlog

# Marks the handler we install so re-configuration (and test harnesses that
# add their own handlers, e.g. pytest's caplog) are never clobbered — we only
# ever remove our own.
_BIFROST_HANDLER_FLAG = "_bifrost_handler"


def _use_json(log_format: str) -> bool:
    if log_format == "json":
        return True
    if log_format == "console":
        return False
    # auto: human-readable on a TTY, structured otherwise.
    return not sys.stderr.isatty()


def configure_logging(settings: object | None = None) -> None:
    """Configure structlog + stdlib logging as one JSON/console pipeline.

    Idempotent: removes only the handler this function installs, so calling it
    from ``create_app()`` repeatedly (and alongside pytest's ``caplog``
    handler) is safe.
    """
    # Imported lazily so this module has no import-time dependency on config
    # (and tests can pass a stub settings object).
    if settings is None:
        from bifrost.cli.config import get_settings

        settings = get_settings()

    level_name: str = getattr(settings, "log_level", "INFO")
    log_format: str = getattr(settings, "log_format", "auto")
    level = logging.getLevelName(level_name)

    # The shared chain run for EVERY record — native structlog and, via
    # ``foreign_pre_chain`` below, foreign stdlib records too. merge_contextvars
    # is first so correlation_id/actor land on every line.
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            # Hand off to the stdlib ProcessorFormatter for final rendering.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer: object
    if _use_json(log_format):
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    formatter = structlog.stdlib.ProcessorFormatter(
        # Foreign (plain stdlib) records get the same context + metadata as
        # native structlog ones before rendering — this is what gives the 100+
        # stdlib loggers their correlation_id and JSON shape.
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler()  # stderr
    handler.setFormatter(formatter)
    setattr(handler, _BIFROST_HANDLER_FLAG, True)

    root = logging.getLogger()
    # Remove only handlers WE previously installed; leave pytest/caplog and any
    # host-provided handlers intact.
    for existing in list(root.handlers):
        if getattr(existing, _BIFROST_HANDLER_FLAG, False):
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
