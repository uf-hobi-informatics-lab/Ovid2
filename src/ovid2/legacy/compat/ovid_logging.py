"""Legacy ``common/ovid_logging.py`` shim (logging calls only)."""

from __future__ import annotations

import logging

_logger = logging.getLogger("ovid2.legacy.formatters")


def get_logger() -> logging.Logger:
    """Return the logger ported formatters log to."""
    return _logger


def log_error(message: str, exc_info: bool = False) -> None:
    """Legacy ``log_error``."""
    _logger.error(message, exc_info=exc_info)


def log_info(message: str) -> None:
    """Legacy ``log_info``."""
    _logger.info(message)


def log_warning(message: str) -> None:
    """Legacy ``log_warning``."""
    _logger.warning(message)
