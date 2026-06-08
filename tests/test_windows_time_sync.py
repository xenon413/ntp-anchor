import types
from types import SimpleNamespace
from ntp_anchor.core import windows_time_sync


def test_start_time_service_already_running(monkeypatch):
    def fake_run(cmd, capture_output=True, text=True):
        return SimpleNamespace(stdout='STATE : 4  RUNNING', returncode=0, stderr='')

    monkeypatch.setattr(windows_time_sync, 'subprocess', SimpleNamespace(run=fake_run))
    assert windows_time_sync.WindowsTimeSync.start_time_service() is True


def test_start_time_service_start(monkeypatch):
    calls = []

    def fake_run(cmd, capture_output=True, text=True):
        calls.append(cmd)
        if cmd == ["sc", "query", "w32time"]:
            return SimpleNamespace(stdout='STATE : 1  STOPPED', returncode=0, stderr='')
        return SimpleNamespace(stdout='SERVICE STARTED', returncode=0, stderr='')

    monkeypatch.setattr(windows_time_sync, 'subprocess', SimpleNamespace(run=fake_run))
    assert windows_time_sync.WindowsTimeSync.start_time_service() is True
    assert calls[0] == ["sc", "query", "w32time"]
    assert calls[1] == ["sc", "start", "w32time"]


def test_sync_time_success(monkeypatch):
    def fake_run(cmd, capture_output=True, text=True):
        return SimpleNamespace(returncode=0, stdout='The command completed successfully.', stderr='')

    monkeypatch.setattr(windows_time_sync, 'subprocess', SimpleNamespace(run=fake_run))
    assert windows_time_sync.WindowsTimeSync.sync_time() is True


def test_sync_time_failure(monkeypatch):
    def fake_run(cmd, capture_output=True, text=True):
        return SimpleNamespace(returncode=1, stdout='', stderr='access denied')

    monkeypatch.setattr(windows_time_sync, 'subprocess', SimpleNamespace(run=fake_run))
    assert windows_time_sync.WindowsTimeSync.sync_time() is False
