"""Linux time-service backend with explicit provider fallback."""

from __future__ import annotations

from collections.abc import Sequence
import logging
import os

from .base_time_sync import BaseTimeSync, normalize_sync_servers
from .command_runner import CommandRunner
from .exceptions import (
    NTPAnchorError,
    TimeServiceCommandError,
    TimeServicePermissionError,
    TimeServiceTimeoutError,
    TimeServiceUnavailableError,
)
from .privileges import TimeSyncPrivilegeCheck, TimeSyncPrivilegeStatus


logger = logging.getLogger(__name__)


def _effective_user_id() -> int:
    """Return the effective POSIX user ID through a testable seam."""

    get_effective_user_id = getattr(os, "geteuid")
    return int(get_effective_user_id())


def detect_time_sync_privilege() -> TimeSyncPrivilegeCheck:
    """Conservatively require a root process for Linux correction commands."""

    try:
        is_elevated = _effective_user_id() == 0
    except Exception as error:
        return TimeSyncPrivilegeCheck(
            TimeSyncPrivilegeStatus.UNKNOWN,
            error_type=type(error).__name__,
        )
    return TimeSyncPrivilegeCheck(
        TimeSyncPrivilegeStatus.AVAILABLE
        if is_elevated
        else TimeSyncPrivilegeStatus.UNAVAILABLE
    )


class LinuxTimeSync(BaseTimeSync):
    """Operate supported Linux time-service providers."""

    _SYSTEMD_SERVICES = (
        "systemd-timesyncd.service",
        "chronyd.service",
        "chrony.service",
        "ntp.service",
        "ntpd.service",
    )
    _SYSV_SERVICES = ("chronyd", "chrony", "ntp", "ntpd")

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self.runner = runner or CommandRunner()

    def _command_exists(self, command: str) -> bool:
        return self.runner.is_available(command)

    def prepare_time_sync(self, servers: Sequence[str]) -> None:
        """Verify an authority-aware one-shot provider without starting a daemon."""

        normalize_sync_servers(servers)
        if any(
            self._command_exists(command) for command in ("ntpdate", "sntp", "chronyd")
        ):
            return
        raise TimeServiceUnavailableError(
            "No Linux synchronization tool accepts explicit NTP authorities."
        )

    def start_time_service(self) -> None:
        """Enable the first Linux time provider that starts successfully."""

        failures: list[NTPAnchorError] = []
        attempted = False

        if self._command_exists("timedatectl"):
            attempted = True
            try:
                self.runner.run("timedatectl", ("set-ntp", "true"))
            except (TimeServicePermissionError, TimeServiceTimeoutError):
                raise
            except NTPAnchorError as error:
                failures.append(error)
            else:
                logger.debug("enabled the Linux NTP provider through timedatectl")
                return

        if self._command_exists("systemctl"):
            attempted = True
            for service in self._SYSTEMD_SERVICES:
                try:
                    self.runner.run("systemctl", ("start", service))
                except (TimeServicePermissionError, TimeServiceTimeoutError):
                    raise
                except NTPAnchorError as error:
                    failures.append(error)
                    continue
                logger.debug("started Linux time provider %s", service)
                return

        if self._command_exists("service"):
            attempted = True
            for service in self._SYSV_SERVICES:
                try:
                    self.runner.run("service", (service, "start"))
                except (TimeServicePermissionError, TimeServiceTimeoutError):
                    raise
                except NTPAnchorError as error:
                    failures.append(error)
                    continue
                logger.debug("started Linux time provider %s", service)
                return

        self._raise_provider_failure(
            "No supported Linux time service could be started.",
            failures,
            attempted=attempted,
        )

    def sync_time(self, servers: Sequence[str] | None = None) -> None:
        """Synchronize through a tool that accepts the selected authorities.

        Provider-managed commands such as ``chronyc makestep`` and
        ``timedatectl set-ntp`` deliberately are not used here: neither can
        guarantee that it will correct against the same authorities measured
        by the controller.
        """

        selected_servers = normalize_sync_servers(servers)
        failures: list[NTPAnchorError] = []
        attempted = False

        if self._command_exists("ntpdate"):
            attempted = True
            try:
                self.runner.run("ntpdate", ("-u", *selected_servers))
            except (TimeServicePermissionError, TimeServiceTimeoutError):
                raise
            except NTPAnchorError as error:
                failures.append(error)
            else:
                logger.info("Linux time synchronization via ntpdate was requested")
                return

        if self._command_exists("sntp"):
            attempted = True
            for server in selected_servers:
                try:
                    self.runner.run("sntp", ("-sS", server))
                except (TimeServicePermissionError, TimeServiceTimeoutError):
                    raise
                except NTPAnchorError as error:
                    failures.append(error)
                    continue
                logger.info("Linux time synchronization via sntp was requested")
                return

        if self._command_exists("chronyd"):
            attempted = True
            directives = tuple(f"server {server} iburst" for server in selected_servers)
            arguments = ["-q"]
            # Let chronyd finish and report before the runner's outer deadline.
            # For very short runner deadlines, omit -t and rely on the runner.
            if self.runner.timeout >= 2.0:
                arguments.extend(("-t", str(max(1, int(self.runner.timeout) - 1))))
            arguments.extend(directives)
            try:
                # chronyd accepts configuration directives as individual
                # command-line arguments. -q corrects once and exits, while -t
                # bounds its own source-acquisition loop in addition to the
                # command runner's outer deadline.
                self.runner.run("chronyd", tuple(arguments))
            except (TimeServicePermissionError, TimeServiceTimeoutError):
                raise
            except NTPAnchorError as error:
                failures.append(error)
            else:
                logger.info("Linux time synchronization via chronyd was requested")
                return

        self._raise_provider_failure(
            "No Linux synchronization tool accepted the configured NTP authorities.",
            failures,
            attempted=attempted,
        )

    @staticmethod
    def _raise_provider_failure(
        message: str,
        failures: Sequence[NTPAnchorError],
        *,
        attempted: bool,
    ) -> None:
        # Preserve actionable classifications after all eligible alternatives
        # have been attempted. Permission and timeout failures take priority.
        for error_type in (
            TimeServicePermissionError,
            TimeServiceTimeoutError,
            TimeServiceCommandError,
            TimeServiceUnavailableError,
        ):
            for error in failures:
                if isinstance(error, error_type):
                    raise error

        if attempted and failures:
            raise failures[-1]
        raise TimeServiceUnavailableError(message)
