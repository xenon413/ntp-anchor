import subprocess

import pytest

from ntp_anchor.core.darwin_time_sync import DarwinTimeSync
from ntp_anchor.core.exceptions import (
    TimeServiceTimeoutError,
    TimeServiceUnavailableError,
)


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
