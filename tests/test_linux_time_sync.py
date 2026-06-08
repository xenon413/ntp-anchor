from types import SimpleNamespace
from ntp_anchor.core import linux_time_sync


def test_command_exists(monkeypatch):
    monkeypatch.setattr(linux_time_sync, 'shutil', SimpleNamespace(which=lambda cmd: '/usr/bin/' + cmd if cmd == 'ntpdate' else None))
    assert linux_time_sync.LinuxTimeSync._command_exists('ntpdate') is True
    assert linux_time_sync.LinuxTimeSync._command_exists('nope') is False


def test_start_time_service_with_timedatectl(monkeypatch):
    monkeypatch.setattr(linux_time_sync.LinuxTimeSync, '_command_exists', staticmethod(lambda cmd: True if cmd in ('timedatectl', 'systemctl') else False))
    monkeypatch.setattr(linux_time_sync, 'subprocess', SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout='', stderr='')))
    assert linux_time_sync.LinuxTimeSync.start_time_service() is True


def test_sync_time_ntpdate(monkeypatch):
    monkeypatch.setattr(linux_time_sync.LinuxTimeSync, '_command_exists', staticmethod(lambda cmd: True if cmd == 'ntpdate' else False))

    def fake_run(cmd, capture_output=True, text=True):
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(linux_time_sync, 'subprocess', SimpleNamespace(run=fake_run))
    assert linux_time_sync.LinuxTimeSync.sync_time() is True


def test_sync_time_no_tools(monkeypatch):
    monkeypatch.setattr(linux_time_sync.LinuxTimeSync, '_command_exists', staticmethod(lambda cmd: False))
    monkeypatch.setattr(linux_time_sync, 'subprocess', SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=1, stdout='', stderr='')))
    assert linux_time_sync.LinuxTimeSync.sync_time() is False
