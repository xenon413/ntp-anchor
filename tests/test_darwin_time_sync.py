from types import SimpleNamespace
from ntp_anchor.core import darwin_time_sync


def test_command_exists(monkeypatch):
    monkeypatch.setattr(darwin_time_sync, 'shutil', SimpleNamespace(which=lambda cmd: '/usr/bin/' + cmd if cmd == 'sntp' else None))
    assert darwin_time_sync.DarwinTimeSync._command_exists('sntp') is True
    assert darwin_time_sync.DarwinTimeSync._command_exists('nope') is False


def test_start_time_service_systemsetup(monkeypatch):
    monkeypatch.setattr(darwin_time_sync.DarwinTimeSync, '_command_exists', staticmethod(lambda cmd: True))
    monkeypatch.setattr(darwin_time_sync, 'subprocess', SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout='', stderr='')))
    assert darwin_time_sync.DarwinTimeSync.start_time_service() is True


def test_sync_time_sntp(monkeypatch):
    monkeypatch.setattr(darwin_time_sync.DarwinTimeSync, '_command_exists', staticmethod(lambda cmd: True if cmd == 'sntp' else False))

    def fake_run(cmd, capture_output=True, text=True):
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(darwin_time_sync, 'subprocess', SimpleNamespace(run=fake_run))
    assert darwin_time_sync.DarwinTimeSync.sync_time() is True


def test_sync_time_failure(monkeypatch):
    monkeypatch.setattr(darwin_time_sync.DarwinTimeSync, '_command_exists', staticmethod(lambda cmd: False))
    monkeypatch.setattr(darwin_time_sync, 'subprocess', SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=1, stdout='', stderr='')))
    assert darwin_time_sync.DarwinTimeSync.sync_time() is False
