import subprocess

import pytest

from ntp_anchor.core.exceptions import (
    TimeServicePermissionError,
    TimeServiceUnavailableError,
)
from ntp_anchor.core.linux_time_sync import LinuxTimeSync


class FakeRunner:
    def __init__(
        self,
        available: set[str],
        *,
        failure: BaseException | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.available = available
        self.failure = failure
        self.timeout = timeout
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def is_available(self, command: str) -> bool:
        return command in self.available

    def run(self, command: str, arguments: tuple[str, ...] = (), **_: object):
        self.calls.append((command, arguments))
        if self.failure is not None:
            raise self.failure
        return subprocess.CompletedProcess(
            args=[command, *arguments],
            returncode=0,
            stdout="",
            stderr="",
        )


@pytest.mark.unit
def test_prepare_uses_one_shot_provider_without_starting_daemon() -> None:
    runner = FakeRunner({"sntp", "systemctl"})

    LinuxTimeSync(runner).prepare_time_sync(("time.example.com",))

    assert runner.calls == []


@pytest.mark.unit
def test_prepare_rejects_daemon_only_environment() -> None:
    runner = FakeRunner({"timedatectl", "systemctl"})

    with pytest.raises(TimeServiceUnavailableError):
        LinuxTimeSync(runner).prepare_time_sync(("time.example.com",))

    assert runner.calls == []


@pytest.mark.unit
def test_permission_failure_aborts_provider_fallback() -> None:
    permission = TimeServicePermissionError(
        command=("ntpdate",),
        returncode=1,
    )
    runner = FakeRunner({"ntpdate", "sntp"}, failure=permission)

    with pytest.raises(TimeServicePermissionError):
        LinuxTimeSync(runner).sync_time(("time.example.com",))

    assert runner.calls == [("ntpdate", ("-u", "time.example.com"))]


@pytest.mark.unit
def test_chronyd_inner_deadline_precedes_runner_deadline() -> None:
    runner = FakeRunner({"chronyd"}, timeout=10.0)

    LinuxTimeSync(runner).sync_time(("time.example.com",))

    assert runner.calls == [
        (
            "chronyd",
            ("-q", "-t", "9", "server time.example.com iburst"),
        )
    ]
