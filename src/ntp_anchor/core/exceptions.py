"""Typed exceptions raised by :mod:`ntp_anchor`.

Exceptions intentionally do not log. The operation boundary that can add
useful context (a worker, CLI, or API) owns logging exactly once.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from typing import Any


_OUTPUT_LIMIT = 2_048


def _command_text(command: Sequence[object] | str | None) -> str:
    if command is None:
        return "<unknown>"
    if isinstance(command, str):
        return command
    return " ".join(str(part) for part in command)


def _bounded(value: object | None) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) <= _OUTPUT_LIMIT:
        return text
    return f"{text[:_OUTPUT_LIMIT]}…"


class NTPAnchorError(Exception):
    """Base exception for all expected ntp-anchor failures."""


class ConfigurationError(NTPAnchorError, ValueError):
    """Raised when runtime configuration is invalid."""


class UnsupportedOSError(NTPAnchorError):
    """Raised when no synchronization backend supports the current OS."""


class UnexpectedError(NTPAnchorError):
    """Compatibility wrapper for an unexpected internal failure."""


class NTPResponseValidationError(NTPAnchorError):
    """Raised when an NTP response is received but is not trustworthy."""


class NTPQueryError(NTPAnchorError):
    """Raised when a configured NTP quorum cannot be obtained."""

    def __init__(
        self,
        attempted_servers: Sequence[str] | Mapping[str, object],
        failures: Mapping[str, object] | None = None,
        *,
        required: int | None = None,
        received: int | None = None,
    ) -> None:
        if isinstance(attempted_servers, Mapping):
            servers = tuple(str(server) for server in attempted_servers)
            failure_map = {
                str(server): _bounded(reason)
                for server, reason in attempted_servers.items()
            }
        else:
            servers = tuple(str(server) for server in attempted_servers)
            failure_map = {
                str(server): _bounded(reason)
                for server, reason in (failures or {}).items()
            }

        quorum = ""
        if required is not None:
            quorum = (
                f" Required {required} valid response(s), received {received or 0}."
            )
        details = ""
        if failure_map:
            rendered = "; ".join(
                f"{server}: {reason or 'unknown failure'}"
                for server, reason in failure_map.items()
            )
            details = f" Failures: {rendered}."

        super().__init__(
            f"Unable to obtain a valid NTP result from [{', '.join(servers)}]."
            f"{quorum}{details}"
        )
        self.attempted_servers = servers
        self.failures = failure_map
        self.required = required
        self.received = received


class UnsafeCorrectionError(NTPAnchorError):
    """Raised when a measured offset exceeds the configured safety limit."""

    def __init__(
        self,
        offset: float | str,
        maximum: float | None = None,
    ) -> None:
        if maximum is None:
            message = str(offset)
            parsed_offset = None
        else:
            parsed_offset = float(offset)
            message = (
                f"Refusing to synchronize offset {parsed_offset:.6f}s; maximum "
                f"allowed correction is {maximum:.6f}s."
            )
        super().__init__(message)
        self.offset = parsed_offset
        self.maximum = maximum


class SyncVerificationError(NTPAnchorError):
    """Raised when the clock does not converge after a sync command."""

    def __init__(
        self,
        offset: float | str,
        threshold: float | None = None,
    ) -> None:
        if threshold is None:
            message = str(offset)
            parsed_offset = None
        else:
            parsed_offset = float(offset)
            message = (
                f"Clock offset {parsed_offset:.6f}s remains outside the "
                f"{threshold:.6f}s threshold after synchronization."
            )
        super().__init__(message)
        self.offset = parsed_offset
        self.threshold = threshold


class TimeServiceUnavailableError(NTPAnchorError):
    """Raised when no usable local time-service provider is available."""


class TimeServiceCommandError(NTPAnchorError):
    """Raised when a local time-service command returns a failure."""

    def __init__(
        self,
        status: subprocess.CompletedProcess[Any] | None = None,
        *,
        command: Sequence[object] | str | None = None,
        returncode: int | None = None,
        stdout: object | None = None,
        stderr: object | None = None,
        message: str | None = None,
    ) -> None:
        if status is not None:
            command = status.args
            returncode = status.returncode
            stdout = status.stdout
            stderr = status.stderr

        self.command = (
            tuple(command)
            if command is not None and not isinstance(command, str)
            else command
        )
        self.returncode = returncode
        self.stdout = _bounded(stdout)
        self.stderr = _bounded(stderr)

        detail = message or self.stderr or self.stdout or "No error details provided."
        super().__init__(
            "Subprocess execution failed: "
            f"command='{_command_text(command)}', exit_code={returncode}, "
            f"details='{detail}'"
        )


class TimeServicePermissionError(TimeServiceCommandError, PermissionError):
    """Raised when elevated permission is required for a time operation."""

    def __init__(
        self,
        status: subprocess.CompletedProcess[Any] | None = None,
        *,
        command: Sequence[object] | str | None = None,
        returncode: int | None = None,
        stdout: object | None = None,
        stderr: object | None = None,
        message: str | None = None,
    ) -> None:
        if status is not None:
            command = status.args
            returncode = status.returncode
            stdout = status.stdout
            stderr = status.stderr

        command_text = _command_text(command)
        compatibility_message = (
            f"Access Denied for {command_text}. Please ensure you are running "
            "as an Administrator or with the required elevated privileges"
        )
        super().__init__(
            command=command,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            message=message or compatibility_message,
        )
        self.args = (message or compatibility_message,)


class TimeServiceTimeoutError(NTPAnchorError, TimeoutError):
    """Raised when a platform command exceeds its deadline."""

    def __init__(self, command: Sequence[object] | str, timeout: float) -> None:
        super().__init__(
            f"Time-service command '{_command_text(command)}' exceeded "
            f"its {timeout:.3f}s timeout."
        )
        self.command = command
        self.timeout = timeout


class WorkerStateError(NTPAnchorError):
    """Raised for an invalid background-worker state transition."""


class WorkerStopTimeoutError(NTPAnchorError, TimeoutError):
    """Raised when the managed worker cannot stop before its deadline."""

    def __init__(self, timeout: float | str) -> None:
        if isinstance(timeout, str):
            message = timeout
            parsed_timeout = None
        else:
            parsed_timeout = float(timeout)
            message = (
                "Background sync worker did not stop within "
                f"{parsed_timeout:.3f}s."
            )
        super().__init__(message)
        self.timeout = parsed_timeout


class PortInUseError(NTPAnchorError):
    """Raised when the management API TCP address is already reserved."""

    def __init__(self, host: str, port: int) -> None:
        endpoint = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        super().__init__(f"API TCP address {endpoint} is already in use.")
        self.host = host
        self.port = port
