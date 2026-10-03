"""Lazy selection of platform-specific time synchronization backends."""

from __future__ import annotations

import platform

from .base_time_sync import BaseTimeSync
from .command_runner import CommandRunner, DEFAULT_COMMAND_TIMEOUT
from .exceptions import UnsupportedOSError


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
        from .windows_time_sync import WindowsTimeSync

        return WindowsTimeSync(
            command_runner,
            service_start_timeout=command_timeout,
        )
    if selected_os == "linux":
        from .linux_time_sync import LinuxTimeSync

        return LinuxTimeSync(command_runner)
    if selected_os == "darwin":
        from .darwin_time_sync import DarwinTimeSync

        return DarwinTimeSync(command_runner)

    raise UnsupportedOSError(
        f"Clock synchronization is not supported on operating system "
        f"{selected_os or '<unknown>'!r}."
    )


__all__ = ["create_time_sync_backend"]
