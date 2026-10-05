from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import pytest

from ntp_anchor._cli import EXIT_OK, EXIT_PORT_IN_USE


pytestmark = [pytest.mark.integration, pytest.mark.server_integration]

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _clean_environment() -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("NTP_ANCHOR_")
    }
    source_path = str(_PROJECT_ROOT / "src")
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path if not existing else os.pathsep.join((source_path, existing))
    )
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def _module_command(port: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "ntp_anchor.api",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]


def _available_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


def _request(
    port: int,
    method: str,
    path: str,
    *,
    timeout: float = 1.0,
) -> tuple[int, dict[str, object]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        return response.status, payload
    finally:
        connection.close()


def _wait_until_ready(
    process: subprocess.Popen[str],
    port: int,
    timeout: float = 10.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(
                "API subprocess exited before readiness "
                f"(code={process.returncode}, stdout={stdout!r}, stderr={stderr!r})"
            )
        try:
            status, payload = _request(port, "GET", "/ping", timeout=0.25)
        except (ConnectionError, OSError, TimeoutError):
            threading.Event().wait(0.025)
            continue
        if status == 200 and payload == {"status": "pong"}:
            return
        pytest.fail(f"unexpected readiness response: {status} {payload!r}")
    pytest.fail("API subprocess did not become ready before its deadline")


def _terminate_process(process: subprocess.Popen[str]) -> tuple[str, str]:
    if process.poll() is None:
        process.terminate()
        try:
            return process.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
    return process.communicate(timeout=5.0)


def test_cli_refuses_a_real_occupied_loopback_port() -> None:
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = int(holder.getsockname()[1])
    try:
        completed = subprocess.run(
            _module_command(port),
            cwd=_PROJECT_ROOT,
            env=_clean_environment(),
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
    finally:
        holder.close()

    assert completed.returncode == EXIT_PORT_IN_USE
    assert f"127.0.0.1:{port}" in completed.stderr
    assert "already in use" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_api_subprocess_ping_status_and_close_exit_gracefully() -> None:
    port = _available_port()
    process = subprocess.Popen(
        _module_command(port),
        cwd=_PROJECT_ROOT,
        env=_clean_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_until_ready(process, port)

        status_code, status = _request(port, "GET", "/status")
        assert status_code == 200
        assert status == {
            "is_running": False,
            "cycle": 300.0,
            "thresh": 0.5,
            "last_checked_at": None,
            "last_synced_at": None,
            "last_check_status": "never",
            "last_measured_offset": None,
        }

        close_code, close_payload = _request(port, "POST", "/close")
        assert close_code == 200
        assert close_payload == {"is_running": False, "is_closing": True}

        stdout, stderr = process.communicate(timeout=10.0)
        assert process.returncode == EXIT_OK, (stdout, stderr)
    finally:
        if process.poll() is None:
            stdout, stderr = _terminate_process(process)
            pytest.fail(
                "API subprocess required forced cleanup "
                f"(stdout={stdout!r}, stderr={stderr!r})"
            )

