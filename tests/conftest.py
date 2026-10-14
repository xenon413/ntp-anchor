from __future__ import annotations

from collections.abc import Sequence
import ipaddress
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from typing import Any

import pytest


LIVE_NETWORK_ENV = "NTP_ANCHOR_RUN_LIVE_NETWORK"
PRIVILEGED_ENV = "NTP_ANCHOR_RUN_PRIVILEGED"
DISPOSABLE_HOST_ENV = "NTP_ANCHOR_DISPOSABLE_HOST"
PRIVILEGED_ACKNOWLEDGEMENT = "I_UNDERSTAND_THIS_CHANGES_SYSTEM_TIME"

_CLOCK_TOOLS = frozenset(
    {
        "chronyd",
        "launchctl",
        "ntpdate",
        "sc",
        "sc.exe",
        "service",
        "sntp",
        "systemctl",
        "systemsetup",
        "timedatectl",
        "w32tm",
        "w32tm.exe",
    }
)


def pytest_addoption(parser: pytest.Parser) -> None:
    safety = parser.getgroup("ntp-anchor safety")
    safety.addoption(
        "--run-live-network",
        action="store_true",
        default=False,
        help=(
            "allow tests marked live_network when the matching environment "
            "guard and an explicit -m expression are also present"
        ),
    )
    safety.addoption(
        "--run-privileged",
        action="store_true",
        default=False,
        help=(
            "allow tests marked privileged only on an acknowledged disposable "
            "host and with an explicit -m expression"
        ),
    )


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    # Configure the base before pytest's temporary-path factory is initialized.
    # Direct children of the project root avoid a shared admin/user temp parent.
    if config.option.basetemp is None:
        managed_tmp = tempfile.TemporaryDirectory(
            prefix=f".pytest-tmp-{os.getpid()}-",
            dir=config.rootpath,
            ignore_cleanup_errors=True,
        )
        config.option.basetemp = managed_tmp.name
        # Config cleanup runs after session teardown even when tests fail; the
        # bound callback also keeps this directory alive until then.
        config.add_cleanup(managed_tmp.cleanup)

    descriptions = {
        "unit": "isolated tests with no real network or privileged operations",
        "integration": "deterministic integration tests using local resources only",
        "server_integration": "loopback-only API subprocess integration tests",
        "live_network": "opt-in tests that contact non-loopback NTP servers",
        "privileged": "opt-in tests permitted to change a disposable host clock",
    }
    for marker, description in descriptions.items():
        config.addinivalue_line("markers", f"{marker}: {description}")


def _explicitly_selected(config: pytest.Config, marker: str) -> bool:
    expression = str(getattr(config.option, "markexpr", "") or "")
    return marker in expression


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    live_enabled = (
        config.getoption("--run-live-network")
        and os.environ.get(LIVE_NETWORK_ENV) == "1"
        and _explicitly_selected(config, "live_network")
    )
    privileged_enabled = (
        config.getoption("--run-privileged")
        and os.environ.get(PRIVILEGED_ENV) == PRIVILEGED_ACKNOWLEDGEMENT
        and os.environ.get(DISPOSABLE_HOST_ENV) == "1"
        and _explicitly_selected(config, "privileged")
    )
    skip_live = pytest.mark.skip(
        reason=(
            "live network tests require -m live_network, --run-live-network, "
            f"and {LIVE_NETWORK_ENV}=1"
        )
    )
    skip_privileged = pytest.mark.skip(
        reason=(
            "privileged tests require -m privileged, --run-privileged, "
            f"{DISPOSABLE_HOST_ENV}=1, and {PRIVILEGED_ENV}="
            f"{PRIVILEGED_ACKNOWLEDGEMENT}"
        )
    )

    for item in items:
        if item.get_closest_marker("privileged") is not None:
            if not privileged_enabled:
                item.add_marker(skip_privileged)
            if item.get_closest_marker("live_network") is not None and not live_enabled:
                item.add_marker(skip_live)
            continue
        if item.get_closest_marker("live_network") is not None and not live_enabled:
            item.add_marker(skip_live)


def _command_name(arguments: Any) -> str | None:
    if isinstance(arguments, (str, bytes, os.PathLike)):
        return Path(os.fsdecode(arguments)).name.casefold()
    if isinstance(arguments, Sequence) and arguments:
        first = arguments[0]
        if isinstance(first, (str, bytes, os.PathLike)):
            return Path(os.fsdecode(first)).name.casefold()
    return None


def _is_loopback_host(host: object) -> bool:
    if host in {None, "", "localhost"}:
        return True
    if not isinstance(host, str):
        return False
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def prevent_unapproved_host_mutation_and_network(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed if an ordinary test reaches a real clock tool or the internet."""

    privileged = (
        request.node.get_closest_marker("privileged") is not None
        and request.config.getoption("--run-privileged")
        and os.environ.get(PRIVILEGED_ENV) == PRIVILEGED_ACKNOWLEDGEMENT
        and os.environ.get(DISPOSABLE_HOST_ENV) == "1"
        and _explicitly_selected(request.config, "privileged")
    )
    live_network = (
        request.node.get_closest_marker("live_network") is not None
        and request.config.getoption("--run-live-network")
        and os.environ.get(LIVE_NETWORK_ENV) == "1"
        and _explicitly_selected(request.config, "live_network")
    )

    if not privileged:
        original_popen = subprocess.Popen
        original_run = subprocess.run

        def guarded_popen(arguments: Any, *args: Any, **kwargs: Any):
            command = _command_name(arguments)
            if command in _CLOCK_TOOLS:
                raise AssertionError(
                    f"unprivileged test attempted real clock tool {command!r}"
                )
            return original_popen(arguments, *args, **kwargs)

        def guarded_run(arguments: Any, *args: Any, **kwargs: Any):
            command = _command_name(arguments)
            if command in _CLOCK_TOOLS:
                raise AssertionError(
                    f"unprivileged test attempted real clock tool {command!r}"
                )
            return original_run(arguments, *args, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", guarded_popen)
        monkeypatch.setattr(subprocess, "run", guarded_run)

    if not live_network:
        original_socket = socket.socket
        original_getaddrinfo = socket.getaddrinfo

        class LoopbackOnlySocket(original_socket):
            def connect(self, address: Any) -> None:
                if self.family in (socket.AF_INET, socket.AF_INET6):
                    if not _is_loopback_host(address[0]):
                        raise AssertionError(
                            f"test attempted non-loopback connection to {address[0]!r}"
                        )
                return super().connect(address)

            def connect_ex(self, address: Any) -> int:
                if self.family in (socket.AF_INET, socket.AF_INET6):
                    if not _is_loopback_host(address[0]):
                        raise AssertionError(
                            f"test attempted non-loopback connection to {address[0]!r}"
                        )
                return super().connect_ex(address)

            def sendto(self, data: bytes, *args: Any) -> int:
                address = args[-1]
                if self.family in (socket.AF_INET, socket.AF_INET6):
                    if not _is_loopback_host(address[0]):
                        raise AssertionError(
                            f"test attempted non-loopback datagram to {address[0]!r}"
                        )
                return super().sendto(data, *args)

        def loopback_getaddrinfo(host: Any, *args: Any, **kwargs: Any):
            if not _is_loopback_host(host):
                raise AssertionError(
                    f"test attempted non-loopback DNS/network access to {host!r}"
                )
            return original_getaddrinfo(host, *args, **kwargs)

        monkeypatch.setattr(socket, "socket", LoopbackOnlySocket)
        monkeypatch.setattr(socket, "getaddrinfo", loopback_getaddrinfo)
