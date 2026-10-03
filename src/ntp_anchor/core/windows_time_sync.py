"""Windows time-service backend."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import logging
import math
import re
import time

from .base_time_sync import BaseTimeSync, normalize_sync_servers
from .command_runner import CommandResult, CommandRunner
from .exceptions import TimeServiceCommandError, TimeServiceTimeoutError


logger = logging.getLogger(__name__)


class WindowsTimeSync(BaseTimeSync):
    """Manage ``w32time`` through resolved Windows system executables."""

    _STOPPED = 1
    _START_PENDING = 2
    _STOP_PENDING = 3
    _RUNNING = 4
    _CONTINUE_PENDING = 5
    _PAUSE_PENDING = 6
    _PAUSED = 7

    _STATE_PATTERN = re.compile(r"(?im)^\s*STATE\s*:\s*([1-7])(?:\s|$)")
    # Label-independent fallback. Exit-code lines are excluded by requiring
    # non-parenthesized text after the numeric value.
    _NUMERIC_STATE_PATTERN = re.compile(r"(?m):\s*([1-7])\s+(?!\()[^\r\n]+$")

    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        service_start_timeout: float = 30.0,
        poll_interval: float = 0.2,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not self._is_positive_finite(service_start_timeout):
            raise ValueError("service_start_timeout must be a finite positive number")
        if not self._is_positive_finite(poll_interval):
            raise ValueError("poll_interval must be a finite positive number")

        self.runner = runner or CommandRunner()
        self.service_start_timeout = float(service_start_timeout)
        self.poll_interval = float(poll_interval)
        self._monotonic = monotonic
        self._sleep = sleep

    @staticmethod
    def _is_positive_finite(value: object) -> bool:
        return (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value))
            and float(value) > 0
        )

    def start_time_service(self) -> None:
        """Start ``w32time`` and wait until its numeric state is RUNNING."""

        logger.debug("ensuring the Windows time service is running")
        deadline = self._monotonic() + self.service_start_timeout
        start_requested = False
        continue_requested = False

        while True:
            status = self.runner.run(
                "sc.exe",
                ("query", "w32time"),
                timeout=self._remaining(deadline),
            )
            state = self._parse_service_state(status)
            if state == self._RUNNING:
                logger.debug("Windows time service is running")
                return

            if state == self._STOPPED and not start_requested:
                self.runner.run(
                    "sc.exe",
                    ("start", "w32time"),
                    timeout=self._remaining(deadline),
                    # Some wrappers expose ERROR_SERVICE_ALREADY_RUNNING as
                    # the process return code. A readiness query remains the
                    # authoritative result in either case.
                    allowed_returncodes=(0, 1056),
                    permission_returncodes=(5,),
                )
                start_requested = True
            elif state == self._PAUSED and not continue_requested:
                self.runner.run(
                    "sc.exe",
                    ("continue", "w32time"),
                    timeout=self._remaining(deadline),
                    allowed_returncodes=(0, 1056),
                    permission_returncodes=(5,),
                )
                continue_requested = True
            elif state not in {
                self._STOPPED,
                self._START_PENDING,
                self._STOP_PENDING,
                self._CONTINUE_PENDING,
                self._PAUSE_PENDING,
                self._PAUSED,
            }:
                raise TimeServiceCommandError(
                    status,
                    message="The Windows time service returned an unknown state.",
                )

            remaining = self._remaining(deadline)
            self._sleep(min(self.poll_interval, remaining))

    def sync_time(self, servers: Sequence[str] | None = None) -> None:
        """Configure optional trusted peers and request a Windows resync.

        Passing ``None`` retains the machine's existing peer configuration for
        backwards compatibility. The managed controller passes its validated
        server collection explicitly so measurement and correction authorities
        remain aligned.
        """

        if servers is not None:
            selected_servers = normalize_sync_servers(servers)
            self._configure_peers(selected_servers)

        status = self.runner.run(
            "w32tm.exe",
            ("/resync",),
            permission_returncodes=(5, 0x80070005, -2147024891),
        )

        # Exit status is the primary signal. These stable legacy fragments
        # catch versions of w32tm known to report a failure with exit code 0;
        # the manager's post-sync NTP verification remains authoritative.
        output = f"{status.stdout}\n{status.stderr}".casefold()
        if "did not resync" in output or "no time data was available" in output:
            raise TimeServiceCommandError(
                status,
                message="Windows accepted the resync request but reported no synchronization.",
            )

        logger.info("Windows time resynchronization was requested successfully")

    def prepare_time_sync(self, servers: Sequence[str]) -> None:
        """Start w32time, then align its peer list with measured inliers."""

        selected_servers = normalize_sync_servers(servers)
        self.start_time_service()
        self._configure_peers(selected_servers)

    def _configure_peers(self, servers: Sequence[str]) -> None:
        peer_list = " ".join(f"{server},0x8" for server in servers)
        self.runner.run(
            "w32tm.exe",
            (
                "/config",
                f"/manualpeerlist:{peer_list}",
                "/syncfromflags:manual",
                "/update",
            ),
            permission_returncodes=(5, 0x80070005, -2147024891),
        )

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise TimeServiceTimeoutError(
                ("sc.exe", "query", "w32time"),
                self.service_start_timeout,
            )
        return remaining

    @classmethod
    def _parse_service_state(cls, status: CommandResult) -> int:
        output = status.stdout or ""
        match = cls._STATE_PATTERN.search(output)
        if match is not None:
            return int(match.group(1))

        candidates = {
            int(match.group(1))
            for match in cls._NUMERIC_STATE_PATTERN.finditer(output)
        }
        if len(candidates) == 1:
            return candidates.pop()

        raise TimeServiceCommandError(
            status,
            message="Unable to determine the numeric Windows time-service state.",
        )
