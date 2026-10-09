from __future__ import annotations

from types import SimpleNamespace
import errno
import socket
from unittest.mock import MagicMock

import pytest

import ntp_anchor.api as api
from ntp_anchor._api_config import (
    validate_api_host,
    validate_api_port,
    validate_log_file,
    validate_log_level,
    validate_server_mode,
)
from ntp_anchor.core.exceptions import (
    ConfigurationError,
    PortInUseError,
    WorkerStateError,
)
from ntp_anchor.core.privileges import (
    TimeSyncPrivilegeCheck,
    TimeSyncPrivilegeStatus,
)


pytestmark = pytest.mark.unit
CORRECTION_ALLOWED = TimeSyncPrivilegeCheck(TimeSyncPrivilegeStatus.AVAILABLE)


@pytest.fixture(autouse=True)
def correction_privilege_detector(
    monkeypatch: pytest.MonkeyPatch,
) -> MagicMock:
    detector = MagicMock(return_value=CORRECTION_ALLOWED)
    monkeypatch.setattr(api, "_detect_time_sync_privilege", detector)
    return detector


def _available_port(host: str, family: socket.AddressFamily) -> int:
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


@pytest.mark.parametrize(
    "host",
    [
        "",
        " localhost",
        "localhost",
        "127.0.0.1 ",
        "http://127.0.0.1",
        "[::1]",
        "0.0.0.0",
        "::",
        "192.0.2.1",
    ],
)
def test_api_host_rejects_ambiguous_or_nonloopback_values(host: str) -> None:
    with pytest.raises(ConfigurationError):
        validate_api_host(host)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1"])
def test_api_host_accepts_explicit_loopback_addresses(host: str) -> None:
    assert validate_api_host(host) == host


@pytest.mark.parametrize("port", [0, 65_536, -1, True, 8000.0, "8000", None])
def test_api_port_rejects_noninteger_and_out_of_range_values(port: object) -> None:
    with pytest.raises(ConfigurationError):
        validate_api_port(port)


@pytest.mark.parametrize("port", [1, 65_535])
def test_api_port_accepts_inclusive_boundaries(port: int) -> None:
    assert validate_api_port(port) == port


@pytest.mark.parametrize(
    ("workers", "reload"),
    [(0, False), (2, False), (True, False), (1, True), (1, 0)],
)
def test_server_mode_enforces_one_worker_without_reload(
    workers: object,
    reload: object,
) -> None:
    with pytest.raises(ConfigurationError):
        validate_server_mode(workers, reload)


def test_server_mode_accepts_only_the_safe_mode() -> None:
    assert validate_server_mode(1, False) is None


@pytest.mark.parametrize("value", ["", " info", "verbose", 1, None])
def test_log_level_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ConfigurationError):
        validate_log_level(value)


def test_log_level_is_case_normalized() -> None:
    assert validate_log_level("WARNING") == "warning"


@pytest.mark.parametrize("value", ["", " logs/api.log", "logs/", "logs\\"])
def test_log_file_rejects_invalid_paths(value: object) -> None:
    with pytest.raises(ConfigurationError):
        validate_log_file(value)


def test_log_file_accepts_relative_and_absolute_file_paths() -> None:
    assert validate_log_file("logs/ntp-anchor.log") == "logs/ntp-anchor.log"
    assert validate_log_file("C:\\service-logs\\ntp-anchor.log") == (
        "C:\\service-logs\\ntp-anchor.log"
    )


def test_bind_server_socket_reserves_ipv4_port_until_closed() -> None:
    host = "127.0.0.1"
    port = _available_port(host, socket.AF_INET)

    listener = api.bind_server_socket(host, port)
    competitor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        assert listener.getsockname()[:2] == (host, port)
        with pytest.raises(OSError):
            competitor.bind((host, port))
    finally:
        competitor.close()
        listener.close()

    replacement = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        replacement.bind((host, port))
    finally:
        replacement.close()


def test_occupied_ipv4_port_raises_explicit_collision() -> None:
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = int(holder.getsockname()[1])
    try:
        with pytest.raises(PortInUseError) as caught:
            api.bind_server_socket("127.0.0.1", port)
    finally:
        holder.close()

    assert caught.value.host == "127.0.0.1"
    assert caught.value.port == port
    assert f"127.0.0.1:{port}" in str(caught.value)


def test_bind_server_socket_supports_ipv6_loopback_when_available() -> None:
    if not socket.has_ipv6:
        pytest.skip("Python reports no IPv6 support")
    try:
        port = _available_port("::1", socket.AF_INET6)
    except OSError as error:
        pytest.skip(f"IPv6 loopback is unavailable: {type(error).__name__}")

    try:
        listener = api.bind_server_socket("::1", port)
    except OSError as error:
        pytest.skip(f"IPv6 loopback cannot listen: {type(error).__name__}")
    try:
        assert listener.family == socket.AF_INET6
        assert listener.getsockname()[0] == "::1"
        assert listener.getsockname()[1] == port
    finally:
        listener.close()


def test_collision_path_closes_partially_created_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = MagicMock()
    listener.bind.side_effect = OSError(errno.EADDRINUSE, "already bound")
    monkeypatch.setattr(api.socket, "socket", MagicMock(return_value=listener))

    with pytest.raises(PortInUseError):
        api.bind_server_socket("127.0.0.1", 9876)

    listener.close.assert_called_once_with()
    listener.listen.assert_not_called()


def test_noncollision_bind_error_is_preserved_and_socket_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = MagicMock()
    failure = OSError(errno.EINVAL, "invalid bind")
    listener.bind.side_effect = failure
    monkeypatch.setattr(api.socket, "socket", MagicMock(return_value=listener))

    with pytest.raises(OSError) as caught:
        api.bind_server_socket("127.0.0.1", 9876)

    assert caught.value is failure
    listener.close.assert_called_once_with()


def test_invalid_bind_configuration_creates_no_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_factory = MagicMock()
    monkeypatch.setattr(api.socket, "socket", socket_factory)

    with pytest.raises(ConfigurationError):
        api.bind_server_socket("0.0.0.0", 8000)
    with pytest.raises(ConfigurationError):
        api.bind_server_socket("127.0.0.1", 0)

    socket_factory.assert_not_called()


def test_port_guard_never_uses_a_tcp_reachability_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = "127.0.0.1"
    port = _available_port(host, socket.AF_INET)
    connect = MagicMock(side_effect=AssertionError("TCP probe must not run"))
    monkeypatch.setattr(socket, "create_connection", connect)

    listener = api.bind_server_socket(host, port)
    try:
        connect.assert_not_called()
    finally:
        listener.close()


def test_run_server_hands_retained_socket_to_uvicorn_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    correction_privilege_detector: MagicMock,
) -> None:
    manager = MagicMock()
    manager.snapshot.return_value = SimpleNamespace(is_running=False)
    listener = MagicMock(spec=socket.socket)
    app = object()
    server = MagicMock()
    config = object()
    config_factory = MagicMock(return_value=config)
    server_factory = MagicMock(return_value=server)
    logging_config = MagicMock(return_value="C:/logs/ntp-anchor.log")
    monkeypatch.setattr(api, "bind_server_socket", MagicMock(return_value=listener))
    monkeypatch.setattr(api, "configure_service_logging", logging_config)
    monkeypatch.setattr(api, "create_app", MagicMock(return_value=app))
    monkeypatch.setattr(api.uvicorn, "Config", config_factory)
    monkeypatch.setattr(api.uvicorn, "Server", server_factory)

    api.run_server(
        manager,
        host="127.0.0.1",
        port=8123,
        log_file="service-logs/api.log",
        log_level="WARNING",
    )

    logging_config.assert_called_once_with("warning", "service-logs/api.log")
    correction_privilege_detector.assert_called_once_with()
    api.create_app.assert_called_once_with(
        manager,
        api.create_app.call_args.args[1],
        allow_unbounded_correction=False,
        time_sync_privilege=CORRECTION_ALLOWED,
    )
    config_factory.assert_called_once_with(
        app=app,
        host="127.0.0.1",
        port=8123,
        workers=1,
        reload=False,
        log_level="warning",
    )
    server_factory.assert_called_once_with(config)
    server.run.assert_called_once_with(sockets=[listener])
    manager.stop.assert_called_once_with()
    listener.close.assert_called_once_with()


def test_run_server_refuses_running_manager_before_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MagicMock()
    manager.snapshot.return_value = SimpleNamespace(is_running=True)
    bind = MagicMock()
    monkeypatch.setattr(api, "bind_server_socket", bind)

    with pytest.raises(WorkerStateError):
        api.run_server(manager)

    bind.assert_not_called()
    manager.stop.assert_not_called()


@pytest.mark.parametrize(
    "options",
    [
        {"workers": 2},
        {"workers": True},
        {"reload": True},
        {"reload": 0},
        {"host": "0.0.0.0"},
        {"port": 0},
        {"log_file": "logs/"},
        {"log_level": "verbose"},
        {"allow_unbounded_correction": 1},
    ],
)
def test_run_server_validates_all_options_before_binding(
    options: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MagicMock()
    bind = MagicMock()
    monkeypatch.setattr(api, "bind_server_socket", bind)

    with pytest.raises(ConfigurationError):
        api.run_server(manager, **options)  # type: ignore[arg-type]

    bind.assert_not_called()
    manager.stop.assert_not_called()


def test_run_server_rejects_unbounded_manager_without_opt_in_before_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MagicMock()
    manager.snapshot.return_value = SimpleNamespace(is_running=False)
    manager.config.max_correction = None
    bind = MagicMock()
    logging_config = MagicMock()
    monkeypatch.setattr(api, "bind_server_socket", bind)
    monkeypatch.setattr(api, "configure_service_logging", logging_config)

    with pytest.raises(ConfigurationError):
        api.run_server(manager)

    bind.assert_not_called()
    logging_config.assert_not_called()


def test_run_server_forwards_unbounded_correction_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MagicMock()
    manager.snapshot.return_value = SimpleNamespace(is_running=False)
    manager.config.max_correction = None
    listener = MagicMock(spec=socket.socket)
    app = object()
    create_app = MagicMock(return_value=app)
    server = MagicMock()
    monkeypatch.setattr(api, "bind_server_socket", MagicMock(return_value=listener))
    monkeypatch.setattr(
        api,
        "configure_service_logging",
        MagicMock(return_value="C:/logs/ntp-anchor.log"),
    )
    monkeypatch.setattr(api, "create_app", create_app)
    monkeypatch.setattr(api.uvicorn, "Config", MagicMock(return_value=object()))
    monkeypatch.setattr(api.uvicorn, "Server", MagicMock(return_value=server))

    api.run_server(manager, allow_unbounded_correction=True)

    create_app.assert_called_once_with(
        manager,
        create_app.call_args.args[1],
        allow_unbounded_correction=True,
        time_sync_privilege=CORRECTION_ALLOWED,
    )
    manager.stop.assert_called_once_with()
    listener.close.assert_called_once_with()


def test_run_server_closes_socket_and_stops_manager_after_server_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MagicMock()
    manager.snapshot.return_value = SimpleNamespace(is_running=False)
    listener = MagicMock(spec=socket.socket)
    server = MagicMock()
    failure = RuntimeError("uvicorn failed")
    server.run.side_effect = failure
    logging_config = MagicMock(return_value="C:/logs/ntp-anchor.log")
    monkeypatch.setattr(api, "bind_server_socket", MagicMock(return_value=listener))
    monkeypatch.setattr(api, "configure_service_logging", logging_config)
    monkeypatch.setattr(api, "create_app", MagicMock(return_value=object()))
    monkeypatch.setattr(api.uvicorn, "Config", MagicMock(return_value=object()))
    monkeypatch.setattr(api.uvicorn, "Server", MagicMock(return_value=server))

    with pytest.raises(RuntimeError) as caught:
        api.run_server(manager)

    assert caught.value is failure
    logging_config.assert_called_once_with("info", "logs/ntp-anchor.log")
    manager.stop.assert_called_once_with()
    listener.close.assert_called_once_with()
