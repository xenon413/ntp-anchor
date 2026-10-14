import subprocess
from unittest.mock import MagicMock, call

import pytest

from ntp_anchor.core.backends import windows as windows_time_sync
from ntp_anchor.core.backends.windows import WindowsTimeSync
from ntp_anchor.core.exceptions import (
    TimeServiceCommandError,
    TimeServicePermissionError,
    TimeServiceTimeoutError,
)
from ntp_anchor.core.privileges import TimeSyncPrivilegeStatus


def _status(
    args: list[str],
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=args,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _service_state(state: int, label: str) -> subprocess.CompletedProcess[str]:
    return _status(
        ["sc.exe", "query", "w32time"],
        stdout=(
            "SERVICE_NAME: w32time\n"
            "        TYPE               : 30  WIN32\n"
            f"        STATE              : {state}  {label}\n"
            "        WIN32_EXIT_CODE    : 0  (0x0)\n"
        ),
    )


def _backend(runner: MagicMock) -> WindowsTimeSync:
    return WindowsTimeSync(
        runner,
        service_start_timeout=1.0,
        poll_interval=0.01,
        monotonic=lambda: 0.0,
        sleep=lambda _: None,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("is_admin", "expected"),
    [
        (True, TimeSyncPrivilegeStatus.AVAILABLE),
        (False, TimeSyncPrivilegeStatus.UNAVAILABLE),
    ],
)
def test_windows_privilege_detection(
    monkeypatch: pytest.MonkeyPatch,
    is_admin: bool,
    expected: TimeSyncPrivilegeStatus,
) -> None:
    monkeypatch.setattr(windows_time_sync, "_is_user_an_admin", lambda: is_admin)

    result = windows_time_sync.detect_time_sync_privilege()

    assert result.status is expected
    assert result.error_type is None


@pytest.mark.unit
def test_windows_privilege_detection_failure_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> bool:
        raise OSError("private detection detail")

    monkeypatch.setattr(windows_time_sync, "_is_user_an_admin", fail)

    result = windows_time_sync.detect_time_sync_privilege()

    assert result.status is TimeSyncPrivilegeStatus.UNKNOWN
    assert result.error_type == "OSError"


@pytest.mark.unit
def test_start_time_service_running() -> None:
    runner = MagicMock()
    runner.run.return_value = _service_state(4, "RUNNING")

    _backend(runner).start_time_service()

    runner.run.assert_called_once_with(
        "sc.exe",
        ("query", "w32time"),
        timeout=1.0,
    )


@pytest.mark.unit
def test_start_time_service_stopped_starts_and_polls_until_running() -> None:
    runner = MagicMock()
    runner.run.side_effect = [
        _service_state(1, "STOPPED"),
        _status(["sc.exe", "start", "w32time"]),
        _service_state(2, "START_PENDING"),
        _service_state(4, "RUNNING"),
    ]

    _backend(runner).start_time_service()

    assert runner.run.call_count == 4
    runner.run.assert_has_calls(
        [
            call("sc.exe", ("query", "w32time"), timeout=1.0),
            call(
                "sc.exe",
                ("start", "w32time"),
                timeout=1.0,
                allowed_returncodes=(0, 1056),
                permission_returncodes=(5,),
            ),
            call("sc.exe", ("query", "w32time"), timeout=1.0),
            call("sc.exe", ("query", "w32time"), timeout=1.0),
        ]
    )


@pytest.mark.unit
def test_start_time_service_permission_error_is_preserved() -> None:
    runner = MagicMock()
    permission_error = TimeServicePermissionError(
        command=("sc.exe", "start", "w32time"),
        returncode=5,
    )
    runner.run.side_effect = [
        _service_state(1, "STOPPED"),
        permission_error,
    ]

    with pytest.raises(TimeServicePermissionError) as caught:
        _backend(runner).start_time_service()

    assert "Access Denied for sc.exe start w32time" in str(caught.value)


@pytest.mark.unit
def test_prepare_time_sync_starts_service_and_configures_consensus_peers() -> None:
    runner = MagicMock()
    runner.run.side_effect = [
        _service_state(4, "RUNNING"),
        _status(["w32tm.exe", "/config"]),
    ]

    _backend(runner).prepare_time_sync(("a.example.com", "b.example.com"))

    runner.run.assert_has_calls(
        [
            call("sc.exe", ("query", "w32time"), timeout=1.0),
            call(
                "w32tm.exe",
                (
                    "/config",
                    "/manualpeerlist:a.example.com,0x8 b.example.com,0x8",
                    "/syncfromflags:manual",
                    "/update",
                ),
                permission_returncodes=(5, 0x80070005, -2147024891),
            ),
        ]
    )


@pytest.mark.unit
def test_sync_time_configures_selected_peers_before_resync() -> None:
    runner = MagicMock()
    runner.run.side_effect = [
        _status(["w32tm.exe", "/config"]),
        _status(
            ["w32tm.exe", "/resync"],
            stdout="The command completed successfully.\n",
        ),
    ]
    backend = _backend(runner)

    backend.sync_time(("time.example.com", "time2.example.com"))

    runner.run.assert_has_calls(
        [
            call(
                "w32tm.exe",
                (
                    "/config",
                    "/manualpeerlist:time.example.com,0x8 time2.example.com,0x8",
                    "/syncfromflags:manual",
                    "/update",
                ),
                permission_returncodes=(5, 0x80070005, -2147024891),
            ),
            call(
                "w32tm.exe",
                ("/resync",),
                permission_returncodes=(5, 0x80070005, -2147024891),
            ),
        ]
    )


@pytest.mark.unit
def test_sync_time_detects_zero_exit_no_time_data_failure() -> None:
    runner = MagicMock()
    runner.run.return_value = _status(
        ["w32tm.exe", "/resync"],
        stdout="The computer did not resync because no time data was available.\n",
    )

    with pytest.raises(TimeServiceCommandError):
        _backend(runner).sync_time()


@pytest.mark.unit
def test_sync_time_permission_error_is_preserved() -> None:
    runner = MagicMock()
    permission_error = TimeServicePermissionError(
        command=("w32tm.exe", "/resync"),
        returncode=0x80070005,
    )
    runner.run.side_effect = permission_error

    with pytest.raises(TimeServicePermissionError) as caught:
        _backend(runner).sync_time()

    assert "Access Denied for w32tm.exe /resync" in str(caught.value)


@pytest.mark.unit
def test_start_time_service_pending_state_has_bounded_readiness_deadline() -> None:
    runner = MagicMock(return_value=_service_state(2, "START_PENDING"))
    runner.run.return_value = _service_state(2, "START_PENDING")
    clock = iter((0.0, 0.0, 1.1))
    backend = WindowsTimeSync(
        runner,
        service_start_timeout=1.0,
        poll_interval=0.01,
        monotonic=lambda: next(clock),
        sleep=lambda _: None,
    )

    with pytest.raises(TimeServiceTimeoutError):
        backend.start_time_service()

    runner.run.assert_called_once_with(
        "sc.exe",
        ("query", "w32time"),
        timeout=1.0,
    )


@pytest.mark.unit
def test_paused_service_is_continued_then_polled_until_running() -> None:
    runner = MagicMock()
    runner.run.side_effect = [
        _service_state(7, "PAUSED"),
        _status(["sc.exe", "continue", "w32time"]),
        _service_state(5, "CONTINUE_PENDING"),
        _service_state(4, "RUNNING"),
    ]

    _backend(runner).start_time_service()

    runner.run.assert_has_calls(
        [
            call("sc.exe", ("query", "w32time"), timeout=1.0),
            call(
                "sc.exe",
                ("continue", "w32time"),
                timeout=1.0,
                allowed_returncodes=(0, 1056),
                permission_returncodes=(5,),
            ),
            call("sc.exe", ("query", "w32time"), timeout=1.0),
            call("sc.exe", ("query", "w32time"), timeout=1.0),
        ]
    )


@pytest.mark.unit
def test_numeric_state_parsing_does_not_require_english_labels() -> None:
    runner = MagicMock()
    runner.run.return_value = _status(
        ["sc.exe", "query", "w32time"],
        stdout=(
            "NOM_SERVICE: w32time\n"
            "        TIPO               : 30  WIN32\n"
            "        ETAT              : 4  EN_COURS\n"
            "        CODE_SORTIE_WIN32 : 0  (0x0)\n"
        ),
    )

    _backend(runner).start_time_service()

    runner.run.assert_called_once()


@pytest.mark.unit
def test_unparseable_service_status_is_a_command_failure() -> None:
    runner = MagicMock()
    runner.run.return_value = _status(
        ["sc.exe", "query", "w32time"],
        stdout="service state unavailable",
    )

    with pytest.raises(TimeServiceCommandError):
        _backend(runner).start_time_service()
