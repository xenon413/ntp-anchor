"""macOS time-service backend with explicit provider fallback."""

from __future__ import annotations

from collections.abc import Sequence
import logging

from .base_time_sync import BaseTimeSync, normalize_sync_servers
from .command_runner import CommandRunner
from .exceptions import (
    NTPAnchorError,
    TimeServiceCommandError,
    TimeServicePermissionError,
    TimeServiceTimeoutError,
    TimeServiceUnavailableError,
)


logger = logging.getLogger(__name__)


class DarwinTimeSync(BaseTimeSync):
    """Operate supported macOS network-time facilities."""

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self.runner = runner or CommandRunner()

    def _command_exists(self, command: str) -> bool:
        return self.runner.is_available(command)

    def prepare_time_sync(self, servers: Sequence[str]) -> None:
        """Verify a provider that can use explicit authorities.

        This deliberately avoids starting ``timed`` with its existing source
        configuration before the measured inlier set has been applied.
        """

        normalize_sync_servers(servers)
        if any(
            self._command_exists(command)
            for command in ("sntp", "ntpdate", "chronyd", "systemsetup")
        ):
            return
        raise TimeServiceUnavailableError(
            "No macOS synchronization tool accepts explicit NTP authorities."
        )

    def start_time_service(self) -> None:
        """Enable network time or raise a typed failure."""

        failures: list[NTPAnchorError] = []
        attempted = False

        if self._command_exists("systemsetup"):
            attempted = True
            try:
                self.runner.run(
                    "systemsetup",
                    ("-setusingnetworktime", "on"),
                )
            except (TimeServicePermissionError, TimeServiceTimeoutError):
                raise
            except NTPAnchorError as error:
                failures.append(error)
            else:
                logger.debug("enabled macOS network time through systemsetup")
                return

        if self._command_exists("launchctl"):
            attempted = True
            try:
                self.runner.run(
                    "launchctl",
                    ("kickstart", "-k", "system/com.apple.timed"),
                )
            except (TimeServicePermissionError, TimeServiceTimeoutError):
                raise
            except NTPAnchorError as error:
                failures.append(error)
            else:
                logger.debug("started the macOS timed service through launchctl")
                return

        self._raise_provider_failure(
            "No supported macOS network-time service is available.",
            failures,
            attempted=attempted,
        )

    def sync_time(self, servers: Sequence[str] | None = None) -> None:
        """Request synchronization against an explicitly selected authority."""

        selected_servers = normalize_sync_servers(servers)
        failures: list[NTPAnchorError] = []
        attempted = False

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
                logger.info("macOS time synchronization via sntp was requested")
                return

        if self._command_exists("ntpdate"):
            attempted = True
            try:
                self.runner.run("ntpdate", ("-u", *selected_servers))
            except (TimeServicePermissionError, TimeServiceTimeoutError):
                raise
            except NTPAnchorError as error:
                failures.append(error)
            else:
                logger.info("macOS time synchronization via ntpdate was requested")
                return

        if self._command_exists("chronyd"):
            attempted = True
            directives = tuple(f"server {server} iburst" for server in selected_servers)
            arguments = ["-q"]
            if self.runner.timeout >= 2.0:
                arguments.extend(("-t", str(max(1, int(self.runner.timeout) - 1))))
            arguments.extend(directives)
            try:
                self.runner.run("chronyd", tuple(arguments))
            except (TimeServicePermissionError, TimeServiceTimeoutError):
                raise
            except NTPAnchorError as error:
                failures.append(error)
            else:
                logger.info("macOS time synchronization via chronyd was requested")
                return

        if self._command_exists("systemsetup"):
            attempted = True
            try:
                # macOS supports one configured network-time authority through
                # systemsetup. The first server is selected deterministically;
                # post-sync measurement decides whether convergence occurred.
                self.runner.run(
                    "systemsetup",
                    ("-setnetworktimeserver", selected_servers[0]),
                )
                self.runner.run(
                    "systemsetup",
                    ("-setusingnetworktime", "on"),
                )
            except (TimeServicePermissionError, TimeServiceTimeoutError):
                raise
            except NTPAnchorError as error:
                failures.append(error)
            else:
                logger.info("macOS network-time synchronization was requested")
                return

        self._raise_provider_failure(
            "No macOS synchronization tool accepted the configured NTP authorities.",
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
