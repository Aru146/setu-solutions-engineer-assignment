"""Structured logging configuration."""

import logging
import sys

from app.config import settings


def setup_logging() -> None:
    """Configure root logger with a consistent format.

    Uses a simple but informative format suitable for both local dev
    and container environments (structured enough for log aggregators
    to parse, human-readable enough for terminal output).
    """
    log_format = (
        "%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d — %(message)s"
    )
    date_format = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(
        level=settings.log_level.upper(),
        format=log_format,
        datefmt=date_format,
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    # Quiet down noisy third-party loggers
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("alembic").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. Use ``__name__`` by convention."""
    return logging.getLogger(name)
