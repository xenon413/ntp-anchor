from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

import ntp_anchor._cli as cli
from ntp_anchor._api_config import (
    DEFAULT_API_HOST,
    DEFAULT_API_LOG_FILE,
    DEFAULT_API_PORT,
)
from ntp_anchor.core.config import SyncConfig
from ntp_anchor.core.exceptions import PortInUseError


pytestmark = pytest.mark.unit


def _namespace(*arguments: str):
    return cli._parser().parse_args(arguments)


def test_launch_settings_use_documented_defaults() -> None:
    settings = cli.resolve_launch_settings(_namespace(), {})
    defaults = SyncConfig()

    assert settings.host == DEFAULT_API_HOST
    assert settings.port == DEFAULT_API_PORT
    assert settings.log_file == DEFAULT_API_LOG_FILE
    assert settings.log_level == "info"
    assert settings.sync_config == defaults


def test_environment_configures_every_supported_setting() -> None:
    settings = cli.resolve_launch_settings(
        _namespace(),
        {
            cli.ENV_API_HOST: "::1",
            cli.ENV_API_PORT: "8123",
            cli.ENV_CYCLE: "45.5",
            cli.ENV_THRESH: "0.25",
            cli.ENV_NTP_SERVERS: "a.example.com, b.example.com",
            cli.ENV_NTP_PORT: "1123",
            cli.ENV_QUORUM: "2",
            cli.ENV_LOG_FILE: "service-logs/api.log",
            cli.ENV_LOG_LEVEL: "WARNING",
        },
    )

    assert settings.host == "::1"
    assert settings.port == 8123
    assert settings.log_file == "service-logs/api.log"
    assert settings.log_level == "warning"
    assert settings.sync_config.servers == (
        "a.example.com",
        "b.example.com",
    )
    assert settings.sync_config.ntp_port == 1123
    assert settings.sync_config.quorum == 2
    assert settings.sync_config.cycle == pytest.approx(45.5)
    assert settings.sync_config.thresh == pytest.approx(0.25)


def test_cli_values_override_environment_values() -> None:
    settings = cli.resolve_launch_settings(
        _namespace(
            "--host",
            "127.0.0.2",
            "--port",
            "9000",
            "--cycle",
            "30",
            "--thresh",
            "0.1",
            "--ntp-server",
            "one.example.com",
            "--ntp-server",
            "two.example.com",
            "--ntp-port",
            "123",
            "--quorum",
            "2",
            "--log-file",
            "cli-logs/api.log",
            "--log-level",
            "debug",
        ),
        {
            cli.ENV_API_HOST: "::1",
            cli.ENV_API_PORT: "8123",
            cli.ENV_CYCLE: "60",
            cli.ENV_THRESH: "1",
            cli.ENV_NTP_SERVERS: "env.example.com",
            cli.ENV_NTP_PORT: "1123",
            cli.ENV_QUORUM: "1",
            cli.ENV_LOG_FILE: "environment-logs/api.log",
            cli.ENV_LOG_LEVEL: "error",
        },
    )

    assert settings.host == "127.0.0.2"
    assert settings.port == 9000
    assert settings.log_file == "cli-logs/api.log"
    assert settings.log_level == "debug"
    assert settings.sync_config == replace(
        SyncConfig(),
        servers=("one.example.com", "two.example.com"),
        ntp_port=123,
        cycle=30.0,
        thresh=0.1,
        quorum=2,
    )


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        (cli.ENV_API_HOST, "0.0.0.0"),
        (cli.ENV_API_PORT, "0"),
        (cli.ENV_API_PORT, "1.5"),
        (cli.ENV_CYCLE, "0"),
        (cli.ENV_CYCLE, "nan"),
        (cli.ENV_THRESH, "-0.1"),
        (cli.ENV_THRESH, "inf"),
        (cli.ENV_NTP_SERVERS, "a.example.com,"),
        (cli.ENV_NTP_PORT, "65536"),
        (cli.ENV_QUORUM, "0"),
        (cli.ENV_LOG_FILE, "logs/"),
        (cli.ENV_LOG_LEVEL, "verbose"),
    ],
)
def test_invalid_environment_fails_before_loading_api_dependencies(
    variable: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    load_runner = MagicMock()
    create_manager = MagicMock()
    monkeypatch.setattr(cli, "_load_server_runner", load_runner)
    monkeypatch.setattr(cli, "create_sync_manager", create_manager)

    result = cli.main([], environ={variable: value})

    assert result == cli.EXIT_INVALID_CONFIGURATION
    assert capsys.readouterr().err.startswith("error: ")
    load_runner.assert_not_called()
    create_manager.assert_not_called()


def test_main_constructs_one_manager_and_forwards_validated_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = object()
    create_manager = MagicMock(return_value=manager)
    run_server = MagicMock()
    monkeypatch.setattr(cli, "create_sync_manager", create_manager)
    monkeypatch.setattr(cli, "_load_server_runner", lambda: run_server)

    result = cli.main(
        [
            "--port",
            "8123",
            "--cycle",
            "60",
            "--thresh",
            "0.25",
            "--ntp-server",
            "time.example.com",
            "--quorum",
            "1",
            "--log-file",
            "service-logs/api.log",
            "--log-level",
            "WARNING",
        ],
        environ={},
    )

    assert result == cli.EXIT_OK
    create_manager.assert_called_once()
    config = create_manager.call_args.args[0]
    assert config.servers == ("time.example.com",)
    assert config.quorum == 1
    assert config.cycle == pytest.approx(60.0)
    assert config.thresh == pytest.approx(0.25)
    run_server.assert_called_once_with(
        manager,
        host="127.0.0.1",
        port=8123,
        workers=1,
        reload=False,
        log_file="service-logs/api.log",
        log_level="warning",
    )


def test_missing_api_extra_has_actionable_exit_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = ModuleNotFoundError("No module named 'fastapi'", name="fastapi")
    monkeypatch.setattr(cli, "_load_server_runner", MagicMock(side_effect=missing))

    result = cli.main([], environ={})

    captured = capsys.readouterr()
    assert result == cli.EXIT_MISSING_API_DEPENDENCIES
    assert "ntp-anchor[api]" in captured.err
    assert "Traceback" not in captured.err


def test_plain_internal_import_failure_is_startup_failure_not_missing_extra(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failure = ImportError("private internal import detail")
    monkeypatch.setattr(cli, "_load_server_runner", MagicMock(side_effect=failure))

    result = cli.main([], environ={})

    captured = capsys.readouterr()
    assert result == cli.EXIT_STARTUP_FAILURE
    assert "ImportError" in captured.err
    assert "private" not in captured.err
    assert "ntp-anchor[api]" not in captured.err
    assert "Traceback" not in captured.err


def test_unrelated_missing_internal_module_is_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failure = ModuleNotFoundError(
        "No module named 'internal_bug'",
        name="internal_bug",
    )
    monkeypatch.setattr(cli, "_load_server_runner", MagicMock(side_effect=failure))

    result = cli.main([], environ={})

    captured = capsys.readouterr()
    assert result == cli.EXIT_STARTUP_FAILURE
    assert "ModuleNotFoundError" in captured.err
    assert "internal_bug" not in captured.err
    assert "ntp-anchor[api]" not in captured.err
    assert "Traceback" not in captured.err


def test_port_collision_has_dedicated_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manager = object()
    run_server = MagicMock(side_effect=PortInUseError("127.0.0.1", 8000))
    monkeypatch.setattr(cli, "create_sync_manager", MagicMock(return_value=manager))
    monkeypatch.setattr(cli, "_load_server_runner", lambda: run_server)

    result = cli.main([], environ={})

    captured = capsys.readouterr()
    assert result == cli.EXIT_PORT_IN_USE
    assert "127.0.0.1:8000" in captured.err
    assert "already in use" in captured.err
    assert "Traceback" not in captured.err


def test_unexpected_startup_failure_is_opaque(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = RuntimeError(r"C:\\private\\tool.exe --password secret")
    monkeypatch.setattr(cli, "create_sync_manager", MagicMock(return_value=object()))
    monkeypatch.setattr(
        cli,
        "_load_server_runner",
        lambda: MagicMock(side_effect=secret),
    )

    result = cli.main([], environ={})

    captured = capsys.readouterr()
    assert result == cli.EXIT_STARTUP_FAILURE
    assert "RuntimeError" in captured.err
    assert "private" not in captured.err
    assert "password" not in captured.err
    assert "Traceback" not in captured.err


def test_keyboard_interrupt_has_conventional_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "create_sync_manager",
        MagicMock(return_value=object()),
    )
    monkeypatch.setattr(
        cli,
        "_load_server_runner",
        lambda: MagicMock(side_effect=KeyboardInterrupt),
    )

    assert cli.main([], environ={}) == cli.EXIT_INTERRUPTED


def test_server_system_exit_is_normalized_to_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "create_sync_manager", MagicMock(return_value=object()))
    monkeypatch.setattr(
        cli,
        "_load_server_runner",
        lambda: MagicMock(side_effect=SystemExit(3)),
    )

    result = cli.main([], environ={})

    captured = capsys.readouterr()
    assert result == cli.EXIT_STARTUP_FAILURE
    assert "SystemExit" in captured.err
    assert "Traceback" not in captured.err


def test_argparse_rejects_unknown_options_before_api_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_runner = MagicMock()
    monkeypatch.setattr(cli, "_load_server_runner", load_runner)

    with pytest.raises(SystemExit) as caught:
        cli.main(["--unknown"], environ={})

    assert caught.value.code == cli.EXIT_INVALID_CONFIGURATION
    load_runner.assert_not_called()


def test_help_and_version_do_not_load_api_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    load_runner = MagicMock()
    monkeypatch.setattr(cli, "_load_server_runner", load_runner)

    with pytest.raises(SystemExit) as help_exit:
        cli.main(["--help"], environ={})
    assert help_exit.value.code == cli.EXIT_OK
    assert "--ntp-server" in capsys.readouterr().out

    with pytest.raises(SystemExit) as version_exit:
        cli.main(["--version"], environ={})
    assert version_exit.value.code == cli.EXIT_OK
    assert "ntp-anchor-api" in capsys.readouterr().out
    load_runner.assert_not_called()

