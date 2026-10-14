from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest


pytestmark = pytest.mark.unit

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_CONFTEST = _PROJECT_ROOT / "tests" / "conftest.py"
_OBSERVATION_ENV = "NTP_ANCHOR_PYTEST_BASETEMP_RECORD"


def _make_isolated_project(base: Path) -> Path:
    project = base / "project"
    tests = project / "tests"
    tests.mkdir(parents=True)
    shutil.copyfile(_SOURCE_CONFTEST, project / "conftest.py")
    (project / "pytest.ini").write_text(
        "[pytest]\naddopts =\ntestpaths = tests\n",
        encoding="utf-8",
    )
    (project / "observer_plugin.py").write_text(
        textwrap.dedent(
            f"""
            import os
            from pathlib import Path

            def pytest_sessionstart(session):
                record = Path(os.environ[{_OBSERVATION_ENV!r}])
                record.write_text(
                    f"{{os.getpid()}}\\n{{session.config.option.basetemp}}\\n",
                    encoding="utf-8",
                )
            """
        ),
        encoding="utf-8",
    )
    return project


def _subprocess_environment(record: Path) -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("NTP_ANCHOR_")
    }
    for name in (
        "COVERAGE_PROCESS_START",
        "PYTHONPATH",
        "PYTEST_ADDOPTS",
        "PYTEST_CURRENT_TEST",
        "PYTEST_DEBUG_TEMPROOT",
        "PYTEST_PLUGINS",
    ):
        environment.pop(name, None)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PYTEST_DEBUG_TEMPROOT"] = str(record.parent)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment[_OBSERVATION_ENV] = str(record)
    return environment


def _run_nested_pytest(
    project: Path,
    record: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "observer_plugin",
            "-q",
            *arguments,
        ],
        cwd=project,
        env=_subprocess_environment(record),
        capture_output=True,
        text=True,
        timeout=30.0,
        check=False,
    )


def _read_observation(record: Path) -> tuple[int, Path]:
    process_id, basetemp = record.read_text(encoding="utf-8").splitlines()
    return int(process_id), Path(basetemp)


def test_managed_basetemp_is_unique_local_and_cleaned_for_all_outcomes(
    tmp_path: Path,
) -> None:
    project = _make_isolated_project(tmp_path)
    sample = project / "tests" / "test_sample.py"
    other_run = project / f".pytest-tmp-{os.getpid()}-other-run"
    other_run.mkdir()
    sentinel = other_run / "keep.txt"
    sentinel.write_text("owned by another run", encoding="utf-8")

    cases = (
        (
            "pass",
            textwrap.dedent(
                """
                from pathlib import Path

                def test_pass(tmp_path, tmp_path_factory, request):
                    actual_basetemp = tmp_path_factory.getbasetemp()
                    configured_basetemp = Path(request.config.option.basetemp)
                    assert actual_basetemp == configured_basetemp
                    assert tmp_path.is_relative_to(actual_basetemp)
                """
            ),
            pytest.ExitCode.OK,
        ),
        (
            "failure",
            "def test_failure(tmp_path):\n    assert False\n",
            pytest.ExitCode.TESTS_FAILED,
        ),
        (
            "collection-error",
            "raise RuntimeError('intentional collection error')\n",
            pytest.ExitCode.INTERRUPTED,
        ),
    )
    observed_paths: list[Path] = []

    for name, source, expected_exit_code in cases:
        sample.write_text(source, encoding="utf-8")
        record = project / f"{name}.txt"

        completed = _run_nested_pytest(project, record)

        assert completed.returncode == expected_exit_code, (
            f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
        )
        process_id, basetemp = _read_observation(record)
        assert basetemp.parent == project
        assert basetemp.name.startswith(f".pytest-tmp-{process_id}-")
        assert not basetemp.exists()
        assert sentinel.read_text(encoding="utf-8") == "owned by another run"
        observed_paths.append(basetemp)

    assert len(set(observed_paths)) == len(cases)


def test_explicit_basetemp_is_not_managed_or_removed(tmp_path: Path) -> None:
    project = _make_isolated_project(tmp_path)
    (project / "tests" / "test_sample.py").write_text(
        textwrap.dedent(
            """
            from pathlib import Path

            def test_explicit_basetemp_is_used(
                tmp_path,
                tmp_path_factory,
                request,
            ):
                actual_basetemp = tmp_path_factory.getbasetemp()
                configured_basetemp = Path(request.config.option.basetemp)
                assert actual_basetemp == configured_basetemp
                assert tmp_path.is_relative_to(actual_basetemp)
                (tmp_path / "retained.txt").write_text(
                    "retained",
                    encoding="utf-8",
                )
            """
        ),
        encoding="utf-8",
    )
    explicit_basetemp = project / "caller-owned-basetemp"
    record = project / "explicit.txt"

    completed = _run_nested_pytest(
        project,
        record,
        "--basetemp",
        str(explicit_basetemp),
    )

    assert completed.returncode == pytest.ExitCode.OK, (
        f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
    )
    _, observed_basetemp = _read_observation(record)
    assert observed_basetemp == explicit_basetemp
    assert explicit_basetemp.is_dir()
    retained_files = list(explicit_basetemp.rglob("retained.txt"))
    assert len(retained_files) == 1
    assert retained_files[0].read_text(encoding="utf-8") == "retained"
