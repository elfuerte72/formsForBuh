"""Structlog configuration + correlation-id context var.

Call :func:`configure_logging` once at app startup. Use :func:`bind_correlation_id`
around the background pipeline so every log line in that task carries the same id.
"""

from __future__ import annotations

import logging
import sys
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

import structlog

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def _add_correlation_id(_, __, event_dict: dict) -> dict:
    cid = _correlation_id.get()
    if cid is not None:
        event_dict.setdefault("correlation_id", cid)
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configure stdlib logging + structlog processors.

    ``fmt='pretty'`` gives coloured, multiline output for dev; ``'json'``
    produces one JSON object per line (for Railway/log aggregators).
    """
    level_no = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    logging.basicConfig(
        stream=sys.stdout,
        level=level_no,
        format="%(message)s",
    )

    processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_correlation_id,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if fmt == "pretty":
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
    else:
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level_no),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name) if name else structlog.get_logger()


@contextmanager
def bind_correlation_id(cid: str | None = None) -> Iterator[str]:
    """Bind a correlation id for the duration of the ``with`` block.

    If ``cid`` is None a new uuid4 is generated. The bound id is visible to all
    structlog loggers via :func:`_add_correlation_id`.
    """
    value = cid or uuid.uuid4().hex
    token = _correlation_id.set(value)
    try:
        yield value
    finally:
        _correlation_id.reset(token)


def current_correlation_id() -> str | None:
    return _correlation_id.get()
