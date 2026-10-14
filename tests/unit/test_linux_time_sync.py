import subprocess

import pytest

from ntp_anchor.core.backends import linux as linux_time_sync
from ntp_anchor.core.backends.linux import LinuxTimeSync
from ntp_anchor.core.exceptions import (
    TimeServiceCommandError,
    TimeServicePermissionError,
    TimeServiceUnavailableError,
)
from ntp_anchor.core.privileges import TimeSyncPrivilegeStatus


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
@pytest.mark.parametrize(
    ("effective_user_id", "expected"),
    [
        (0, TimeSyncPrivilegeStatus.AVAILABLE),
        (1_000, TimeSyncPrivilegeStatus.UNAVAILABLE),
    ],
)
def test_linux_privilege_detection(
    monkeypatch: pytest.MonkeyPatch,
    effective_user_id: int,
    expected: TimeSyncPrivilegeStatus,
) -> None:
    monkeypatch.setattr(
        linux_time_sync,
        "_effective_user_id",
        lambda: effective_user_id,
    )

    result = linux_time_sync.detect_time_sync_privilege()

    assert result.status is expected
    assert result.error_type is None


@pytest.mark.unit
def test_linux_privilege_detection_failure_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> int:
        raise RuntimeError("private detection detail")

    monkeypatch.setattr(linux_time_sync, "_effective_user_id", fail)

    result = linux_time_sync.detect_time_sync_privilege()

    assert result.status is TimeSyncPrivilegeStatus.UNKNOWN
    assert result.error_type == "RuntimeError"


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


@pytest.mark.unit
def test_command_failure_falls_back_to_next_authority_aware_provider() -> None:
    class FallbackRunner(FakeRunner):
        def run(self, command: str, arguments: tuple[str, ...] = (), **_: object):
            self.calls.append((command, arguments))
            if command == "ntpdate":
                raise TimeServiceCommandError(command=(command,), returncode=1)
            return subprocess.CompletedProcess(
                args=[command, *arguments],
                returncode=0,
                stdout="",
                stderr="",
            )

    runner = FallbackRunner({"ntpdate", "sntp"})

    LinuxTimeSync(runner).sync_time(("time.example.com",))

    assert runner.calls == [
        ("ntpdate", ("-u", "time.example.com")),
        ("sntp", ("-sS", "time.example.com")),
    ]


@pytest.mark.unit
def test_no_eligible_provider_is_typed_unavailable() -> None:
    runner = FakeRunner(set())

    with pytest.raises(TimeServiceUnavailableError):
        LinuxTimeSync(runner).sync_time(("time.example.com",))

    assert runner.calls == []
