"""Bounded, injectable execution of trusted operating-system commands.

Platform adapters use this module instead of invoking ``subprocess`` directly.
Executables are resolved only from administrator-controlled system directories,
commands never pass through a shell, and every invocation has a deadline.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
import math
import os
from pathlib import Path
import signal
import shutil
import stat
import subprocess
import threading
from typing import BinaryIO, TypeAlias

from ..exceptions import (
    TimeServiceCommandError,
    TimeServicePermissionError,
    TimeServiceTimeoutError,
    TimeServiceUnavailableError,
)


DEFAULT_COMMAND_TIMEOUT = 15.0
DEFAULT_MAX_OUTPUT_CHARS = 64 * 1024

CommandResult: TypeAlias = subprocess.CompletedProcess[str]
Executor: TypeAlias = Callable[..., subprocess.CompletedProcess[str]]
Resolver: TypeAlias = Callable[[str], str | None]


class _BoundedBytes:
    """Retain a fixed-size head and tail while a pipe is drained."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._head_limit = limit // 2
        self._tail_limit = limit - self._head_limit
        self._head = bytearray()
        self._tail = bytearray()
        self._overflowed = False

    def append(self, chunk: bytes) -> None:
        if not self._overflowed:
            combined = self._head + chunk
            if len(combined) <= self._limit:
                self._head = combined
                return
            self._overflowed = True
            self._head = combined[: self._head_limit]
            self._tail = combined[-self._tail_limit :]
            return

        combined_tail = self._tail + chunk
        self._tail = combined_tail[-self._tail_limit :]

    def render(self) -> str:
        if not self._overflowed:
            return bytes(self._head).decode(errors="replace")
        marker = b"\n...[command output truncated]...\n"
        value = bytes(self._head) + marker + bytes(self._tail)
        return value.decode(errors="replace")[: self._limit]


def _trusted_directories() -> tuple[Path, ...]:
    """Return system-owned executable directories for the current platform."""

    if os.name == "nt":
        system_directory: Path | None = None
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(32_768)
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            length = kernel32.GetSystemDirectoryW(
                buffer,
                len(buffer),
            )
            if 0 < length < len(buffer):
                system_directory = Path(buffer.value)
        except (AttributeError, OSError, ValueError):
            system_directory = None

        if system_directory is None:
            system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
            system_directory = system_root / "System32"
        else:
            system_root = system_directory.parent
        return (
            system_directory,
            system_root / "Sysnative",
        )

    return tuple(
        Path(directory)
        for directory in (
            "/usr/sbin",
            "/usr/bin",
            "/sbin",
            "/bin",
            "/usr/local/sbin",
            "/usr/local/bin",
        )
    )


def _is_trusted_executable(path: Path, trusted_directories: set[Path]) -> bool:
    """Validate location and, on POSIX, system ownership/write permissions."""

    if path.parent not in trusted_directories or not path.is_file():
        return False
    if not os.access(path, os.X_OK):
        return False
    if os.name == "nt":
        return True

    try:
        file_status = path.stat()
        directory_status = path.parent.stat()
    except OSError:
        return False

    unsafe_write_bits = stat.S_IWGRP | stat.S_IWOTH
    return (
        file_status.st_uid == 0
        and directory_status.st_uid == 0
        and not file_status.st_mode & unsafe_write_bits
        and not directory_status.st_mode & unsafe_write_bits
    )


def resolve_system_executable(command: str) -> str | None:
    """Resolve *command* without consulting an untrusted caller ``PATH``.

    Absolute paths are accepted only when they point to an executable regular
    file. Bare command names are searched in a fixed set of conventional,
    administrator-controlled directories.
    """

    if not command or "\x00" in command:
        return None

    trusted_directories = {
        directory.resolve(strict=False) for directory in _trusted_directories()
    }
    candidate = Path(command)
    if candidate.is_absolute():
        resolved_candidate = candidate.resolve(strict=False)
        if _is_trusted_executable(resolved_candidate, trusted_directories):
            return str(resolved_candidate)
        return None

    if candidate.name != command:
        return None

    search_path = os.pathsep.join(
        str(directory) for directory in _trusted_directories()
    )
    resolved = shutil.which(command, path=search_path)
    if resolved is None:
        return None

    resolved_path = Path(resolved).resolve(strict=False)
    if not _is_trusted_executable(resolved_path, trusted_directories):
        return None
    return str(resolved_path)


def _minimal_environment() -> dict[str, str]:
    """Build the small environment needed by the supported system tools."""

    trusted_path = os.pathsep.join(
        str(directory) for directory in _trusted_directories()
    )
    environment = {"PATH": trusted_path}

    if os.name == "nt":
        for name in ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP"):
            value = os.environ.get(name)
            if value:
                environment[name] = value
    else:
        # Stable diagnostics make classification and testing less locale-sensitive.
        environment.update({"LANG": "C", "LC_ALL": "C"})

    return environment


class CommandRunner:
    """Run resolved commands with deadlines and bounded pipe consumption.

    ``resolver`` and ``executor`` are injectable for deterministic unit tests.
    Production execution drains stdout and stderr concurrently while retaining
    only a fixed-size head and tail from each stream.
    """

    _PERMISSION_RETURN_CODES = frozenset(
        {
            5,  # ERROR_ACCESS_DENIED
            126,  # POSIX command found but not executable
            0x80070005,  # HRESULT_FROM_WIN32(ERROR_ACCESS_DENIED)
            -2147024891,  # signed representation of 0x80070005
        }
    )

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        resolver: Resolver | None = None,
        executor: Executor | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("command timeout must be a finite positive number")
        if not math.isfinite(float(timeout)) or float(timeout) <= 0:
            raise ValueError("command timeout must be a finite positive number")
        if isinstance(max_output_chars, bool) or not isinstance(max_output_chars, int):
            raise ValueError("max_output_chars must be a positive integer")
        if max_output_chars <= 0:
            raise ValueError("max_output_chars must be a positive integer")

        self.timeout = float(timeout)
        self.max_output_chars = max_output_chars
        self._resolver = resolver or resolve_system_executable
        self._executor = executor
        self._environment = (
            dict(environment) if environment is not None else _minimal_environment()
        )

    def resolve(self, command: str) -> str | None:
        """Return the trusted absolute path for *command*, if available."""

        return self._resolver(command)

    def is_available(self, command: str) -> bool:
        """Return whether *command* resolves to a trusted executable."""

        return self.resolve(command) is not None

    def run(
        self,
        command: str,
        arguments: Sequence[str] = (),
        *,
        timeout: float | None = None,
        allowed_returncodes: Iterable[int] = (0,),
        permission_returncodes: Iterable[int] = (),
    ) -> CommandResult:
        """Execute a system command or raise a typed time-service error."""

        resolved = self.resolve(command)
        if resolved is None:
            raise TimeServiceUnavailableError(
                f"Required system command is unavailable: {command!r}."
            )

        normalized_arguments = self._validate_arguments(arguments)
        command_line = (resolved, *normalized_arguments)
        effective_timeout = (
            self.timeout if timeout is None else self._validate_timeout(timeout)
        )
        accepted_codes = frozenset(int(code) for code in allowed_returncodes)
        permission_codes = self._PERMISSION_RETURN_CODES | frozenset(
            int(code) for code in permission_returncodes
        )

        try:
            if self._executor is None:
                status = self._execute_bounded(command_line, effective_timeout)
            else:
                status = self._executor(
                    list(command_line),
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=effective_timeout,
                    check=False,
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    close_fds=True,
                    env=self._environment.copy(),
                )
        except subprocess.TimeoutExpired as error:
            raise TimeServiceTimeoutError(command_line, effective_timeout) from error
        except PermissionError as error:
            raise TimeServicePermissionError(
                command=command_line,
                returncode=getattr(error, "errno", None),
                stderr=str(error),
            ) from error
        except FileNotFoundError as error:
            # The executable may have disappeared after resolution.
            raise TimeServiceUnavailableError(
                f"Required system command became unavailable: {command!r}."
            ) from error
        except OSError as error:
            raise TimeServiceCommandError(
                command=command_line,
                returncode=getattr(error, "errno", None),
                stderr=str(error),
                message=f"Unable to execute system command {command!r}.",
            ) from error

        status.stdout = self._truncate_output(status.stdout)
        status.stderr = self._truncate_output(status.stderr)

        unsigned_returncode = int(status.returncode) & 0xFFFFFFFF
        combined_output = f"{status.stdout}\n{status.stderr}".casefold()
        permission_markers = (
            "permission denied",
            "access is denied",
            "access denied",
            "must be root",
            "not authorized",
            "authentication is required",
            "interactive authentication required",
            "administrator access",
            "operation not permitted",
        )
        if (
            int(status.returncode) in permission_codes
            or unsigned_returncode in permission_codes
            or "0x80070005" in combined_output
            or any(marker in combined_output for marker in permission_markers)
        ):
            raise TimeServicePermissionError(status)

        if int(status.returncode) not in accepted_codes:
            raise TimeServiceCommandError(status)

        return status

    def _execute_bounded(
        self,
        command_line: Sequence[str],
        timeout: float,
    ) -> CommandResult:
        stdout = _BoundedBytes(self.max_output_chars)
        stderr = _BoundedBytes(self.max_output_chars)
        popen_options: dict[str, object] = {}
        if os.name == "nt":
            popen_options["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            popen_options["start_new_session"] = True

        process = subprocess.Popen(
            list(command_line),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            env=self._environment.copy(),
            **popen_options,
        )
        assert process.stdout is not None
        assert process.stderr is not None

        readers = (
            threading.Thread(
                target=self._drain_pipe,
                args=(process.stdout, stdout),
                name="ntp-anchor-command-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=self._drain_pipe,
                args=(process.stderr, stderr),
                name="ntp-anchor-command-stderr",
                daemon=True,
            ),
        )
        for reader in readers:
            reader.start()

        timed_out = False
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate_process_tree(process)
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
            returncode = process.returncode if process.returncode is not None else -1
        finally:
            for reader in readers:
                reader.join(timeout=1.0)
            if readers[0].is_alive():
                process.stdout.close()
            if readers[1].is_alive():
                process.stderr.close()
            for reader in readers:
                if reader.is_alive():
                    reader.join(timeout=0.2)

        if timed_out:
            raise subprocess.TimeoutExpired(
                list(command_line),
                timeout,
                output=stdout.render(),
                stderr=stderr.render(),
            )

        return subprocess.CompletedProcess(
            args=list(command_line),
            returncode=returncode,
            stdout=stdout.render(),
            stderr=stderr.render(),
        )

    def _terminate_process_tree(self, process: subprocess.Popen[bytes]) -> None:
        """Best-effort bounded termination of the command and its descendants."""

        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
                return
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    process.kill()
                except OSError:
                    pass
                return

        taskkill = resolve_system_executable("taskkill.exe")
        if taskkill is not None:
            try:
                subprocess.run(
                    [taskkill, "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    close_fds=True,
                    env=self._environment.copy(),
                    timeout=5.0,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                return
            except (OSError, subprocess.TimeoutExpired):
                pass

        try:
            process.kill()
        except OSError:
            pass

    @staticmethod
    def _drain_pipe(stream: BinaryIO, destination: _BoundedBytes) -> None:
        try:
            while chunk := stream.read(8 * 1024):
                destination.append(chunk)
        finally:
            stream.close()

    @staticmethod
    def _validate_arguments(arguments: Sequence[str]) -> tuple[str, ...]:
        if isinstance(arguments, (str, bytes)):
            raise TypeError(
                "command arguments must be a sequence of individual strings"
            )

        normalized: list[str] = []
        for argument in arguments:
            if not isinstance(argument, str):
                raise TypeError("every command argument must be a string")
            if "\x00" in argument:
                raise ValueError("command arguments cannot contain NUL characters")
            normalized.append(argument)
        return tuple(normalized)

    @staticmethod
    def _validate_timeout(timeout: float) -> float:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("command timeout must be a finite positive number")
        value = float(timeout)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("command timeout must be a finite positive number")
        return value

    def _truncate_output(self, output: str | bytes | None) -> str:
        if output is None:
            return ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        if len(output) <= self.max_output_chars:
            return output

        marker = "\n...[command output truncated]...\n"
        retained = max(0, self.max_output_chars - len(marker))
        leading = retained // 2
        trailing = retained - leading
        suffix = output[-trailing:] if trailing else ""
        return f"{output[:leading]}{marker}{suffix}"[: self.max_output_chars]
