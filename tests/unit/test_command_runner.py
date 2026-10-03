import subprocess
from unittest.mock import MagicMock

import pytest

from ntp_anchor.core.command_runner import CommandRunner
from ntp_anchor.core.exceptions import (
    TimeServicePermissionError,
    TimeServiceTimeoutError,
    TimeServiceUnavailableError,
)


@pytest.mark.unit
def test_runner_uses_resolved_path_deadline_and_no_shell() -> None:
    executor = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=["/trusted/tool", "arg"],
            returncode=0,
            stdout="ok",
            stderr="",
        )
    )
    runner = CommandRunner(
        timeout=3.0,
        resolver=lambda command: "/trusted/tool" if command == "tool" else None,
        executor=executor,
        environment={"PATH": "/trusted"},
    )

    result = runner.run("tool", ("arg",))

    assert result.stdout == "ok"
    executor.assert_called_once_with(
        ["/trusted/tool", "arg"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=3.0,
        check=False,
        shell=False,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        env={"PATH": "/trusted"},
    )


@pytest.mark.unit
def test_runner_rejects_unresolved_command() -> None:
    runner = CommandRunner(resolver=lambda _: None)

    with pytest.raises(TimeServiceUnavailableError):
        runner.run("missing")


@pytest.mark.unit
def test_runner_translates_timeout() -> None:
    executor = MagicMock(side_effect=subprocess.TimeoutExpired(["tool"], 1.0))
    runner = CommandRunner(
        resolver=lambda _: "/trusted/tool",
        executor=executor,
    )

    with pytest.raises(TimeServiceTimeoutError):
        runner.run("tool", timeout=1.0)


@pytest.mark.unit
def test_runner_classifies_permission_text() -> None:
    executor = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=["/trusted/tool"],
            returncode=1,
            stdout="",
            stderr="Interactive authentication required.",
        )
    )
    runner = CommandRunner(
        resolver=lambda _: "/trusted/tool",
        executor=executor,
    )

    with pytest.raises(TimeServicePermissionError):
        runner.run("tool")


@pytest.mark.unit
def test_runner_bounds_injected_executor_output() -> None:
    executor = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=["/trusted/tool"],
            returncode=0,
            stdout="a" * 1_000,
            stderr="b" * 1_000,
        )
    )
    runner = CommandRunner(
        max_output_chars=100,
        resolver=lambda _: "/trusted/tool",
        executor=executor,
    )

    result = runner.run("tool")

    assert len(result.stdout) <= 100
    assert len(result.stderr) <= 100
    assert "truncated" in result.stdout
