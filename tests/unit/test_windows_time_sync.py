from ntp_anchor.core.windows_time_sync import WindowsTimeSync
import ntp_anchor
from unittest.mock import MagicMock, call
import pytest

# for type hint
from pytest import MonkeyPatch 
from pytest_mock import MockerFixture

# use for test if monkeypatch actually does it's job right
from ntp_anchor.core.linux_time_sync import LinuxTimeSync


# ---- test start time service----
@pytest.mark.unit
def test_start_time_service_running(mocker:MockerFixture, monkeypatch:MonkeyPatch):
    mock_run = mocker.patch("subprocess.run")
    monkeypatch.setattr(ntp_anchor.core.core_logic, "OS", "windows")
    monkeypatch.setattr(ntp_anchor.core, "service", WindowsTimeSync)

    query_mock = MagicMock(
        stdout=(
            "\n"
            "SERVICE_NAME: w32time\n"
            "        TYPE               : 30  WIN32\n"
            "        STATE              : 4  RUNNING\n"
            "                                (STOPPABLE, NOT_PAUSABLE, ACCEPTS_SHUTDOWN)\n"
            "        WIN32_EXIT_CODE    : 0  (0x0)\n"
            "        SERVICE_EXIT_CODE  : 0  (0x0)\n"
            "        CHECKPOINT         : 0x0\n"
            "        WAIT_HINT          : 0x0\n"
        ),
        args=['sc.exe', 'query', 'w32time'],
        returncode=0,
        stderr="",
    )

    mock_run.return_value = query_mock
    ntp_anchor.start_time_service()

    mock_run.assert_called_once_with(
        ["sc.exe", "query", "w32time"], 
        capture_output=True, 
        text=True
    )

@pytest.mark.unit
def test_start_time_service_stopped_admin(mocker:MockerFixture, monkeypatch:MonkeyPatch):
    mock_run = mocker.patch("subprocess.run")
    monkeypatch.setattr(ntp_anchor.core.core_logic, "OS", "windows")
    monkeypatch.setattr(ntp_anchor.core, "service", WindowsTimeSync)

    query_mock = MagicMock(
        stdout=(
            "\n"
            "SERVICE_NAME: w32time\n"
            "        TYPE               : 30  WIN32\n"
            "        STATE              : 1  STOPPED\n"
            "                                (STOPPABLE, NOT_PAUSABLE, ACCEPTS_SHUTDOWN)\n"
            "        WIN32_EXIT_CODE    : 0  (0x0)\n"
            "        SERVICE_EXIT_CODE  : 0  (0x0)\n"
            "        CHECKPOINT         : 0x0\n"
            "        WAIT_HINT          : 0x0\n"
        ),
        args=["sc.exe", "query", "w32time"],
        returncode=0,
        stderr="",
    )

    start_mock = MagicMock(
        stdout=(
            "\n"
            "SERVICE_NAME: w32time\n"
            "        TYPE               : 30  WIN32\n"
            "        STATE              : 2  START_PENDING\n"
            "                                (NOT_STOPPABLE, NOT_PAUSABLE, IGNORES_SHUTDOWN)\n"
            "        WIN32_EXIT_CODE    : 0  (0x0)\n"
            "        SERVICE_EXIT_CODE  : 0  (0x0)\n"
            "        CHECKPOINT         : 0x0\n"
            "        WAIT_HINT          : 0x7d0\n" # 2s
            "        PID                : 1560\n"
            "        FLAGS              :\n"
        ),
        args=["sc.exe", "start", "w32time"],
        returncode=0,
        stderr="",
    )

    mock_run.side_effect = [query_mock, start_mock]

    ntp_anchor.start_time_service()

    expected_sequence = [
        call(["sc.exe", "query", "w32time"], capture_output=True, text=True),
        call(["sc.exe", "start", "w32time"], capture_output=True, text=True)
    ]

    assert mock_run.call_count == 2
    mock_run.assert_has_calls(expected_sequence, any_order=False)

@pytest.mark.unit
def test_start_time_service_stopped_no_admin(mocker:MockerFixture, monkeypatch:MonkeyPatch):
    mock_run = mocker.patch("subprocess.run")
    monkeypatch.setattr(ntp_anchor.core.core_logic, "OS", "windows")
    monkeypatch.setattr(ntp_anchor.core, "service", WindowsTimeSync)

    query_mock = MagicMock(
        stdout=(
            "\n"
            "SERVICE_NAME: w32time\n"
            "        TYPE               : 30  WIN32\n"
            "        STATE              : 1  STOPPED\n"
            "                                (STOPPABLE, NOT_PAUSABLE, ACCEPTS_SHUTDOWN)\n"
            "        WIN32_EXIT_CODE    : 0  (0x0)\n"
            "        SERVICE_EXIT_CODE  : 0  (0x0)\n"
            "        CHECKPOINT         : 0x0\n"
            "        WAIT_HINT          : 0x0\n"
        ),
        args=["sc.exe", "query", "w32time"],
        returncode=0,
        stderr="",
    )

    start_mock = MagicMock(
        stdout=(
            "[SC] StartService: OpenService FAILED 5:\n"
            "\n"
            "Access is denied.\n"
            "\n"
        ),
        args=["sc.exe", "start", "w32time"],
        returncode=5,
        stderr="",
    )
    
    mock_run.side_effect = [query_mock, start_mock]
    with pytest.raises(ntp_anchor.exceptions.TimeServicePermissionError) as exc_info:
        ntp_anchor.start_time_service()

    assert f"Access Denied for sc.exe start w32time. Please ensure you are running as an Administrator" in str(exc_info.value)

    expected_sequence = [
        call(["sc.exe", "query", "w32time"], capture_output=True, text=True),
        call(["sc.exe", "start", "w32time"], capture_output=True, text=True)
    ]

    assert mock_run.call_count == 2
    mock_run.assert_has_calls(expected_sequence, any_order=False)


# TODO: 
# test edge case when hits start/stop pending and 

# ---- test time sync ----
@pytest.mark.unit
def test_sync_time_admin(mocker:MockerFixture, monkeypatch:MonkeyPatch):
    mock_run = mocker.patch("subprocess.run")
    monkeypatch.setattr(ntp_anchor.core.core_logic, "OS", "windows")
    monkeypatch.setattr(ntp_anchor.core, "service", WindowsTimeSync)
    
    sync_mock = MagicMock(
        stdout=(
            "Sending resync command to local computer\n"
            "The command completed successfully.\n"
        ),
        args=['w32tm', '/resync'],
        returncode=0,
        stderr=""
    )

    mock_run.return_value = sync_mock
    ntp_anchor.sync_time()
    mock_run.assert_called_once_with(
        ['w32tm', '/resync'], 
        capture_output=True, 
        text=True
    )

@pytest.mark.unit
def test_sync_time_admin_no_wifi(mocker:MockerFixture, monkeypatch:MonkeyPatch):
    mock_run = mocker.patch("subprocess.run")
    monkeypatch.setattr(ntp_anchor.core.core_logic, "OS", "windows")
    monkeypatch.setattr(ntp_anchor.core, "service", WindowsTimeSync)

    sync_mock = MagicMock(
        stdout=(
            "Sending resync command to local computer\n"
            "The computer did not resync because no time data was available.\n"
        ),
        args=['w32tm', '/resync'],
        returncode=0,
        stderr=""
    )

    mock_run.return_value = sync_mock
    with pytest.raises(ntp_anchor.exceptions.TimeServiceCommandError):
        ntp_anchor.sync_time()
    
    mock_run.assert_called_once_with(
        ['w32tm', '/resync'], 
        capture_output=True, 
        text=True
    )

@pytest.mark.unit
def test_sync_time_no_admin(mocker:MockerFixture, monkeypatch:MonkeyPatch):
    mock_run = mocker.patch("subprocess.run")
    monkeypatch.setattr(ntp_anchor.core.core_logic, "OS", "windows")
    monkeypatch.setattr(ntp_anchor.core, "service", WindowsTimeSync)
    sync_mock = MagicMock(
        stdout=(
            "Sending resync command to local computer\n"
            "The following error occurred: Access is denied. (0x80070005)\n"
        ),
        args=['w32tm', '/resync'],
        returncode=2147942405,
        stderr=""
    )
    mock_run.return_value = sync_mock
    with pytest.raises(ntp_anchor.exceptions.TimeServicePermissionError) as exc_info:
        ntp_anchor.sync_time()

    assert f"Access Denied for w32tm /resync. Please ensure you are running as an Administrator" in str(exc_info.value)
    
    mock_run.assert_called_once_with(
        ['w32tm', '/resync'], 
        capture_output=True, 
        text=True
    )

# TODO:
# test sync when win32time is stopped
