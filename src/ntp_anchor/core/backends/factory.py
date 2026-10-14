"""Lazy selection of platform-specific time synchronization backends."""

from __future__ import annotations

import platform

from .base import BaseTimeSync
from .command_runner import CommandRunner, DEFAULT_COMMAND_TIMEOUT
from ..exceptions import UnsupportedOSError
from ..privileges import TimeSyncPrivilegeCheck, TimeSyncPrivilegeStatus


def detect_time_sync_privilege(
    os_name: str | None = None,
) -> TimeSyncPrivilegeCheck:
    """Run the selected platform's non-mutating privilege detector."""

    try:
        selected_os = (os_name or platform.system()).strip().casefold()
        if selected_os == "windows":
            from .windows import (
                detect_time_sync_privilege as detect_windows_privilege,
            )

            check = detect_windows_privilege()
        elif selected_os == "linux":
            from .linux import (
                detect_time_sync_privilege as detect_linux_privilege,
            )

            check = detect_linux_privilege()
        elif selected_os == "darwin":
            from .darwin import (
                detect_time_sync_privilege as detect_darwin_privilege,
            )

            check = detect_darwin_privilege()
        else:
            return TimeSyncPrivilegeCheck(
                TimeSyncPrivilegeStatus.UNKNOWN,
                error_type="UnsupportedOSError",
            )
        if not isinstance(check, TimeSyncPrivilegeCheck):
            raise TypeError("platform privilege detector returned an invalid result")
        return check
    except Exception as error:
        return TimeSyncPrivilegeCheck(
            TimeSyncPrivilegeStatus.UNKNOWN,
            error_type=type(error).__name__,
        )


def create_time_sync_backend(
    os_name: str | None = None,
    *,
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    runner: CommandRunner | None = None,
) -> BaseTimeSync:
    """Create a backend only when a privileged operation actually needs one.

    Keeping platform imports inside this function allows read-only NTP
    measurement to work on systems without a supported clock backend.
    """

    selected_os = (os_name or platform.system()).strip().casefold()
    command_runner = runner or CommandRunner(timeout=command_timeout)

    if selected_os == "windows":
        from .windows import WindowsTimeSync

        return WindowsTimeSync(
            command_runner,
            service_start_timeout=command_timeout,
        )
    if selected_os == "linux":
        from .linux import LinuxTimeSync

        return LinuxTimeSync(command_runner)
    if selected_os == "darwin":
        from .darwin import DarwinTimeSync

        return DarwinTimeSync(command_runner)

    raise UnsupportedOSError(
        f"Clock synchronization is not supported on operating system "
        f"{selected_os or '<unknown>'!r}."
    )


__all__ = ["create_time_sync_backend", "detect_time_sync_privilege"]
