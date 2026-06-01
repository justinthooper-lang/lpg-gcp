"""Structured logging configuration for the LPG webhook handler.

Uses structlog. Auto-detects local dev (TTY → pretty colored output)
vs production (no TTY → JSON for Cloud Logging ingestion).

Call configure_logging() once at app startup. Then anywhere in the
code:

    import structlog
    log = structlog.get_logger()
    log.info("order_ingested", order_id=31990, items=1)
"""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog and stdlib logging.

    Args:
        level: log level name (DEBUG, INFO, WARNING, ERROR).
    """
    # stdlib logging — uvicorn/FastAPI emit via stdlib, so we route
    # both through the same renderer.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper()),
    )

    is_tty = sys.stderr.isatty()

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if is_tty:
        # Local dev: human-readable colored output
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(colors=True),
        ]
    else:
        # Production / Cloud Run: JSON for Cloud Logging
        processors = shared_processors + [
            structlog.processors.JSONRenderer(),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper())
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    