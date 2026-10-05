from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from ntp_anchor._cli import EXIT_MISSING_API_DEPENDENCIES


pytestmark = pytest.mark.unit

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    source_path = str(_PROJECT_ROOT / "src")
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path if not existing else os.pathsep.join((source_path, existing))
    )
    return environment


def test_imports_do_not_parse_arguments_bind_start_threads_or_spawn_commands() -> None:
    script = textwrap.dedent(
        """
        import os
        import socket
        import subprocess
        import sys
        import threading

        class GuardSocket(socket.socket):
            def bind(self, *args, **kwargs):
                raise AssertionError("import attempted to bind a socket")
            def connect(self, *args, **kwargs):
                raise AssertionError("import attempted to connect a socket")
            def connect_ex(self, *args, **kwargs):
                raise AssertionError("import attempted to connect a socket")
            def sendto(self, *args, **kwargs):
                raise AssertionError("import attempted to send a datagram")

        def forbidden_start(self):
            raise AssertionError("import attempted to start a thread")

        def forbidden_process(*args, **kwargs):
            raise AssertionError("import attempted to spawn a process")

        def forbidden_exit(*args, **kwargs):
            raise AssertionError("import attempted abrupt process exit")

        socket.socket = GuardSocket
        threading.Thread.start = forbidden_start
        # Keep Popen as a class: asyncio.windows_utils subclasses it during
        # Uvicorn's cold import on Windows.  Guarding construction detects a
        # real process spawn without breaking that ordinary class definition.
        subprocess.Popen.__init__ = forbidden_process
        subprocess.run = forbidden_process
        os._exit = forbidden_exit
        sys.argv = ["import-smoke", "--an-option-that-must-not-be-parsed"]

        before = tuple(thread.name for thread in threading.enumerate())
        import ntp_anchor
        import ntp_anchor.api
        import ntp_anchor._cli
        after = tuple(thread.name for thread in threading.enumerate())
        assert before == after
        assert ntp_anchor.__version__
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_PROJECT_ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=15.0,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""


def test_core_and_cli_import_without_api_extra_and_cli_explains_install() -> None:
    script = textwrap.dedent(
        """
        import importlib.abc
        import sys

        blocked = {"fastapi", "pydantic", "starlette", "uvicorn"}

        class BlockOptionalAPI(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path, target=None):
                top_level = fullname.split(".", 1)[0]
                if top_level in blocked:
                    raise ModuleNotFoundError(
                        f"No module named {top_level!r}",
                        name=top_level,
                    )
                return None

        sys.meta_path.insert(0, BlockOptionalAPI())
        import ntp_anchor
        from ntp_anchor import _cli

        assert ntp_anchor.__version__
        raise SystemExit(_cli.main([], environ={}))
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_PROJECT_ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=15.0,
        check=False,
    )

    assert completed.returncode == EXIT_MISSING_API_DEPENDENCIES
    assert "ntp-anchor[api]" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_module_invocation_without_api_extra_uses_same_actionable_exit(
    tmp_path: Path,
) -> None:
    blocked_api = tmp_path / "fastapi"
    blocked_api.mkdir()
    (blocked_api / "__init__.py").write_text(
        "raise ModuleNotFoundError("
        '"No module named \'fastapi\'", name="fastapi")\n',
        encoding="utf-8",
    )
    environment = _environment()
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(tmp_path), environment["PYTHONPATH"])
    )

    completed = subprocess.run(
        [sys.executable, "-m", "ntp_anchor.api"],
        cwd=_PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15.0,
        check=False,
    )

    assert completed.returncode == EXIT_MISSING_API_DEPENDENCIES
    assert "ntp-anchor[api]" in completed.stderr
    assert "Traceback" not in completed.stderr

