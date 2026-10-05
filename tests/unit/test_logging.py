from __future__ import annotations

from io import StringIO
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import time

import pytest

from ntp_anchor._logging import (
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_MAX_BYTES,
    UTCFormatter,
    configure_service_logging,
)


pytestmark = pytest.mark.unit


def _restore_logger(
    logger: logging.Logger,
    handlers: list[logging.Handler],
    level: int,
    disabled: bool,
    propagate: bool,
) -> None:
    for handler in tuple(logger.handlers):
        if handler not in handlers:
            logger.removeHandler(handler)
            handler.close()
    logger.handlers[:] = handlers
    logger.setLevel(level)
    logger.disabled = disabled
    logger.propagate = propagate


def test_service_logging_writes_utc_stdout_and_rotating_file(
    tmp_path: Path,
) -> None:
    package_logger = logging.getLogger("ntp_anchor")
    original_handlers = list(package_logger.handlers)
    original_level = package_logger.level
    original_disabled = package_logger.disabled
    original_propagate = package_logger.propagate
    original_root_handlers = tuple(logging.getLogger().handlers)
    original_converter = logging.Formatter.converter
    output = StringIO()
    log_file = tmp_path / "logs" / "ntp-anchor.log"

    try:
        configured_path = configure_service_logging(
            "debug",
            str(log_file),
            stream=output,
        )
        package_logger.getChild("test").debug("logging probe")
        for handler in package_logger.handlers:
            handler.flush()

        assert configured_path == log_file.resolve()
        assert log_file.exists()
        assert "logging probe" in output.getvalue()
        assert "logging probe" in log_file.read_text(encoding="utf-8")
        assert tuple(logging.getLogger().handlers) == original_root_handlers
        assert logging.Formatter.converter is original_converter

        rotating = [
            handler
            for handler in package_logger.handlers
            if isinstance(handler, RotatingFileHandler)
        ]
        assert len(rotating) == 1
        assert rotating[0].maxBytes == DEFAULT_LOG_MAX_BYTES
        assert rotating[0].backupCount == DEFAULT_LOG_BACKUP_COUNT
        assert isinstance(rotating[0].formatter, UTCFormatter)
        assert rotating[0].formatter.converter is time.gmtime
    finally:
        _restore_logger(
            package_logger,
            original_handlers,
            original_level,
            original_disabled,
            original_propagate,
        )


def test_service_logging_reconfiguration_replaces_owned_handlers(
    tmp_path: Path,
) -> None:
    package_logger = logging.getLogger("ntp_anchor")
    original_handlers = list(package_logger.handlers)
    original_level = package_logger.level
    original_disabled = package_logger.disabled
    original_propagate = package_logger.propagate

    try:
        configure_service_logging(
            "info",
            str(tmp_path / "first.log"),
            stream=StringIO(),
        )
        configure_service_logging(
            "warning",
            str(tmp_path / "second.log"),
            stream=StringIO(),
            max_bytes=1_024,
            backup_count=2,
        )

        owned = [
            handler
            for handler in package_logger.handlers
            if getattr(handler, "_ntp_anchor_service_handler", False)
        ]
        assert len(owned) == 2
        assert package_logger.level == logging.WARNING
        rotating = next(
            handler
            for handler in owned
            if isinstance(handler, RotatingFileHandler)
        )
        assert rotating.maxBytes == 1_024
        assert rotating.backupCount == 2
    finally:
        _restore_logger(
            package_logger,
            original_handlers,
            original_level,
            original_disabled,
            original_propagate,
        )


@pytest.mark.parametrize(
    ("option", "value", "error_type"),
    [
        ("max_bytes", 0, ValueError),
        ("max_bytes", True, TypeError),
        ("backup_count", 0, ValueError),
        ("backup_count", False, TypeError),
    ],
)
def test_service_logging_rejects_invalid_rotation_settings(
    tmp_path: Path,
    option: str,
    value: object,
    error_type: type[Exception],
) -> None:
    arguments = {option: value}
    with pytest.raises(error_type):
        configure_service_logging(
            "info",
            str(tmp_path / "ntp-anchor.log"),
            **arguments,  # type: ignore[arg-type]
        )
