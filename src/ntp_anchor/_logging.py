"""Service-owned logging configuration with no import-time side effects."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import time
from typing import TextIO

from ._api_config import validate_log_file, validate_log_level


DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 10

_PACKAGE_LOGGER_NAME = "ntp_anchor"
_OWNED_HANDLER_ATTRIBUTE = "_ntp_anchor_service_handler"
_LOG_LEVEL_VALUES = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
    "trace": 5,
}


class UTCFormatter(logging.Formatter):
    """Format timestamps in UTC without changing global logging behavior."""

    converter = time.gmtime


def _owned(handler: logging.Handler) -> bool:
    return bool(getattr(handler, _OWNED_HANDLER_ATTRIBUTE, False))


def _mark_owned(handler: logging.Handler) -> None:
    setattr(handler, _OWNED_HANDLER_ATTRIBUTE, True)


def configure_service_logging(
    log_level: str,
    log_file: str,
    *,
    stream: TextIO | None = None,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> Path:
    """Configure stdout and rotating-file handlers for the service namespace.

    Repeated calls replace only handlers installed by this function. Host
    applications embedding :func:`ntp_anchor.api.create_app` retain ownership
    of their logging because this function is called only by the server starter.
    """

    normalized_level = validate_log_level(log_level)
    validated_file = validate_log_file(log_file)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise TypeError("max_bytes must be an integer")
    if max_bytes <= 0:
        raise ValueError("max_bytes must be greater than zero")
    if isinstance(backup_count, bool) or not isinstance(backup_count, int):
        raise TypeError("backup_count must be an integer")
    if backup_count < 1:
        raise ValueError("backup_count must be at least one")

    path = Path(validated_file).expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)

    formatter = UTCFormatter(
        (
            "%(asctime)s | %(process)d | %(threadName)s | %(name)s | "
            "%(levelname)s | %(message)s"
        ),
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    file_handler = RotatingFileHandler(
        filename=path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    output_handler = logging.StreamHandler(sys.stdout if stream is None else stream)
    for handler in (file_handler, output_handler):
        handler.setLevel(_LOG_LEVEL_VALUES[normalized_level])
        handler.setFormatter(formatter)
        _mark_owned(handler)

    package_logger = logging.getLogger(_PACKAGE_LOGGER_NAME)
    previous_handlers = tuple(
        handler for handler in package_logger.handlers if _owned(handler)
    )
    for handler in previous_handlers:
        package_logger.removeHandler(handler)
    for handler in previous_handlers:
        handler.close()

    logging.addLevelName(_LOG_LEVEL_VALUES["trace"], "TRACE")
    package_logger.setLevel(_LOG_LEVEL_VALUES[normalized_level])
    package_logger.disabled = False
    package_logger.propagate = False
    package_logger.addHandler(output_handler)
    package_logger.addHandler(file_handler)
    package_logger.info(
        "service logging initialized log_file=%s max_bytes=%d backup_count=%d",
        path,
        max_bytes,
        backup_count,
    )
    return path


__all__ = [
    "DEFAULT_LOG_BACKUP_COUNT",
    "DEFAULT_LOG_MAX_BYTES",
    "UTCFormatter",
    "configure_service_logging",
]
