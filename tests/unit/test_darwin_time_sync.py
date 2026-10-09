import subprocess

import pytest

from ntp_anchor.core import darwin_time_sync
from ntp_anchor.core.darwin_time_sync import DarwinTimeSync
from ntp_anchor.core.exceptions import (
    TimeServiceCommandError,
    TimeServicePermissionError,
    TimeServiceTimeoutError,
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
        (501, TimeSyncPrivilegeStatus.UNAVAILABLE),
    ],
)
def test_darwin_privilege_detection(
    monkeypatch: pytest.MonkeyPatch,
    effective_user_id: int,
    expected: TimeSyncPrivilegeStatus,
) -> None:
    monkeypatch.setattr(
        darwin_time_sync,
        "_effective_user_id",
        lambda: effective_user_id,
    )

    result = darwin_time_sync.detect_time_sync_privilege()

    assert result.status is expected
    assert result.error_type is None


@pytest.mark.unit
def test_darwin_privilege_detection_failure_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> int:
        raise OSError("private detection detail")

    monkeypatch.setattr(darwin_time_sync, "_effective_user_id", fail)

    result = darwin_time_sync.detect_time_sync_privilege()

    assert result.status is TimeSyncPrivilegeStatus.UNKNOWN
    assert result.error_type == "OSError"


@pytest.mark.unit
def test_prepare_does_not_start_timed_with_stale_sources() -> None:
    runner = FakeRunner({"sntp", "launchctl"})

    DarwinTimeSync(runner).prepare_time_sync(("time.example.com",))

    assert runner.calls == []


@pytest.mark.unit
def test_prepare_requires_authority_aware_provider() -> None:
    runner = FakeRunner({"launchctl"})

    with pytest.raises(TimeServiceUnavailableError):
        DarwinTimeSync(runner).prepare_time_sync(("time.example.com",))


@pytest.mark.unit
def test_timeout_aborts_sntp_fallback() -> None:
    timeout = TimeServiceTimeoutError(("sntp",), 1.0)
    runner = FakeRunner({"sntp", "ntpdate"}, failure=timeout)

    with pytest.raises(TimeServiceTimeoutError):
        DarwinTimeSync(runner).sync_time(("a.example.com", "b.example.com"))

    assert runner.calls == [("sntp", ("-sS", "a.example.com"))]


@pytest.mark.unit
def test_command_failure_falls_back_across_servers_and_providers() -> None:
    class FallbackRunner(FakeRunner):
        def run(self, command: str, arguments: tuple[str, ...] = (), **_: object):
            self.calls.append((command, arguments))
            if command == "sntp":
                raise TimeServiceCommandError(command=(command,), returncode=1)
            return subprocess.CompletedProcess(
                args=[command, *arguments],
                returncode=0,
                stdout="",
                stderr="",
            )

    runner = FallbackRunner({"sntp", "ntpdate"})

    DarwinTimeSync(runner).sync_time(("a.example.com", "b.example.com"))

    assert runner.calls == [
        ("sntp", ("-sS", "a.example.com")),
        ("sntp", ("-sS", "b.example.com")),
        ("ntpdate", ("-u", "a.example.com", "b.example.com")),
    ]


@pytest.mark.unit
def test_permission_failure_aborts_fallback() -> None:
    permission = TimeServicePermissionError(command=("sntp",), returncode=1)
    runner = FakeRunner({"sntp", "ntpdate"}, failure=permission)

    with pytest.raises(TimeServicePermissionError):
        DarwinTimeSync(runner).sync_time(("time.example.com",))

    assert runner.calls == [("sntp", ("-sS", "time.example.com"))]
