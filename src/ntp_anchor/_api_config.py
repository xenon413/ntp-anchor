"""Dependency-free management-server configuration validation.

This module is intentionally separate from :mod:`ntp_anchor.api` so the
console entry point can validate every option before importing the optional
FastAPI/Uvicorn stack.
"""

from __future__ import annotations

import ipaddress

from .core.exceptions import ConfigurationError


DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8000
DEFAULT_API_LOG_LEVEL = "info"
DEFAULT_API_LOG_FILE = "logs/ntp-anchor.log"
MAX_CYCLE_SECONDS = 86_400.0
UVICORN_LOG_LEVELS = frozenset(
    {"critical", "error", "warning", "info", "debug", "trace"}
)


def validate_api_host(host: object) -> str:
    """Return a normalized loopback IP address accepted by the launcher."""

    if not isinstance(host, str) or not host or host != host.strip():
        raise ConfigurationError("API host must be a non-empty string")
    if any(character.isspace() or ord(character) < 32 for character in host):
        raise ConfigurationError(
            "API host cannot contain whitespace or control characters"
        )
    if "://" in host or "/" in host or "@" in host or "[" in host or "]" in host:
        raise ConfigurationError("API host must be an unbracketed IP address")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ConfigurationError(
            "API host must be an explicit loopback IP address"
        ) from error
    if not address.is_loopback:
        raise ConfigurationError(
            "non-loopback API binding requires an authenticated trusted "
            "deployment, which is not enabled by this launcher"
        )
    return str(address)


def validate_api_port(port: object) -> int:
    """Validate a TCP port without accepting Boolean integer subclasses."""

    if isinstance(port, bool) or not isinstance(port, int):
        raise ConfigurationError("API port must be an integer")
    if not 1 <= port <= 65_535:
        raise ConfigurationError("API port must be between 1 and 65535")
    return port


def validate_server_mode(workers: object, reload: object) -> None:
    """Enforce the single-process, no-reload lifecycle invariant."""

    if isinstance(workers, bool) or not isinstance(workers, int):
        raise ConfigurationError("Uvicorn workers must be an integer")
    if workers != 1:
        raise ConfigurationError("ntp-anchor requires exactly one Uvicorn worker")
    if not isinstance(reload, bool):
        raise ConfigurationError("Uvicorn reload must be a Boolean")
    if reload:
        raise ConfigurationError("Uvicorn auto-reload is not supported")


def validate_allow_unbounded_correction(value: object) -> bool:
    """Validate the explicit service capability for disabling the hard guard."""

    if not isinstance(value, bool):
        raise ConfigurationError("allow_unbounded_correction must be a Boolean")
    return value


def validate_log_level(log_level: object) -> str:
    """Return a normalized service-wide log level."""

    if not isinstance(log_level, str) or not log_level:
        raise ConfigurationError("log level must be a non-empty string")
    if log_level != log_level.strip():
        raise ConfigurationError("log level cannot contain surrounding whitespace")
    normalized = log_level.lower()
    if normalized not in UVICORN_LOG_LEVELS:
        choices = ", ".join(sorted(UVICORN_LOG_LEVELS))
        raise ConfigurationError(f"log level must be one of: {choices}")
    return normalized


def validate_log_file(log_file: object) -> str:
    """Validate the rotating service log path without touching the filesystem."""

    if not isinstance(log_file, str) or not log_file:
        raise ConfigurationError("log file must be a non-empty path string")
    if log_file != log_file.strip():
        raise ConfigurationError("log file cannot contain surrounding whitespace")
    if any(ord(character) < 32 for character in log_file):
        raise ConfigurationError("log file cannot contain control characters")
    if log_file in {".", ".."} or log_file.endswith(("/", "\\")):
        raise ConfigurationError("log file must identify a file, not a directory")
    return log_file


__all__ = [
    "DEFAULT_API_HOST",
    "DEFAULT_API_LOG_FILE",
    "DEFAULT_API_LOG_LEVEL",
    "DEFAULT_API_PORT",
    "MAX_CYCLE_SECONDS",
    "UVICORN_LOG_LEVELS",
    "validate_allow_unbounded_correction",
    "validate_api_host",
    "validate_api_port",
    "validate_log_file",
    "validate_log_level",
    "validate_server_mode",
]
