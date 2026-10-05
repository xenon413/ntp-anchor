"""Dependency-light command-line entry point for the management API."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
import os
import sys
from typing import Any, Callable

from . import __version__
from ._api_config import (
    DEFAULT_API_HOST,
    DEFAULT_API_LOG_FILE,
    DEFAULT_API_LOG_LEVEL,
    DEFAULT_API_PORT,
    MAX_CYCLE_SECONDS,
    UVICORN_LOG_LEVELS,
    validate_api_host,
    validate_api_port,
    validate_log_file,
    validate_log_level,
)
from .core.config import SyncConfig
from .core.core_logic import create_sync_manager
from .core.exceptions import ConfigurationError, PortInUseError


EXIT_OK = 0
EXIT_STARTUP_FAILURE = 1
EXIT_INVALID_CONFIGURATION = 2
EXIT_MISSING_API_DEPENDENCIES = 3
EXIT_PORT_IN_USE = 4
EXIT_INTERRUPTED = 130

ENV_API_HOST = "NTP_ANCHOR_API_HOST"
ENV_API_PORT = "NTP_ANCHOR_API_PORT"
ENV_CYCLE = "NTP_ANCHOR_CYCLE"
ENV_THRESH = "NTP_ANCHOR_THRESH"
ENV_NTP_SERVERS = "NTP_ANCHOR_NTP_SERVERS"
ENV_NTP_PORT = "NTP_ANCHOR_NTP_PORT"
ENV_QUORUM = "NTP_ANCHOR_QUORUM"
ENV_LOG_FILE = "NTP_ANCHOR_LOG_FILE"
ENV_LOG_LEVEL = "NTP_ANCHOR_LOG_LEVEL"

_OPTIONAL_API_MODULES = frozenset({"fastapi", "pydantic", "starlette", "uvicorn"})


@dataclass(frozen=True, slots=True)
class ApiLaunchSettings:
    """Fully validated inputs required to construct and run one API server."""

    host: str
    port: int
    sync_config: SyncConfig
    log_file: str
    log_level: str


def _parser() -> argparse.ArgumentParser:
    defaults = SyncConfig()
    parser = argparse.ArgumentParser(
        prog="ntp-anchor-api",
        description=(
            "Run the loopback-only ntp-anchor management API with one "
            "Uvicorn worker. CLI values override environment values."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--host",
        metavar="IP",
        help=(
            f"loopback bind address ({ENV_API_HOST}; default: {DEFAULT_API_HOST})"
        ),
    )
    parser.add_argument(
        "--port",
        metavar="PORT",
        help=f"API TCP port ({ENV_API_PORT}; default: {DEFAULT_API_PORT})",
    )
    parser.add_argument(
        "--cycle",
        metavar="SECONDS",
        help=(
            f"periodic check interval, > 0 and <= {MAX_CYCLE_SECONDS:g} "
            f"({ENV_CYCLE}; default: {defaults.cycle:g})"
        ),
    )
    parser.add_argument(
        "--thresh",
        metavar="SECONDS",
        help=(
            "non-negative drift threshold, no greater than max correction "
            f"({ENV_THRESH}; default: {defaults.thresh:g})"
        ),
    )
    parser.add_argument(
        "--ntp-server",
        action="append",
        dest="ntp_servers",
        metavar="HOST",
        help=(
            "trusted NTP hostname/IP; repeat to configure multiple sources "
            f"({ENV_NTP_SERVERS}: comma-separated; default: "
            f"{','.join(defaults.servers)})"
        ),
    )
    parser.add_argument(
        "--ntp-port",
        metavar="PORT",
        help=f"remote NTP UDP port ({ENV_NTP_PORT}; default: {defaults.ntp_port})",
    )
    parser.add_argument(
        "--quorum",
        metavar="COUNT",
        help=(
            "required agreeing NTP responses "
            f"({ENV_QUORUM}; default: {defaults.quorum})"
        ),
    )
    parser.add_argument(
        "--log-file",
        metavar="PATH",
        help=(
            "rotating service log path "
            f"({ENV_LOG_FILE}; default: {DEFAULT_API_LOG_FILE})"
        ),
    )
    parser.add_argument(
        "--log-level",
        metavar="LEVEL",
        help=(
            f"service-wide log level ({ENV_LOG_LEVEL}; default: "
            f"{DEFAULT_API_LOG_LEVEL}; choices: "
            f"{', '.join(sorted(UVICORN_LOG_LEVELS))})"
        ),
    )
    return parser


def _select(
    cli_value: Any,
    environment: Mapping[str, str],
    variable: str,
    default: Any,
) -> Any:
    if cli_value is not None:
        return cli_value
    if variable in environment:
        return environment[variable]
    return default


def _integer(value: object, *, name: str) -> int:
    if isinstance(value, bool):
        raise ConfigurationError(f"{name} must be an integer")
    if isinstance(value, int):
        return value
    if not isinstance(value, str) or not value or value != value.strip():
        raise ConfigurationError(f"{name} must be an integer")
    try:
        return int(value, 10)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an integer") from error


def _finite_number(
    value: object,
    *,
    name: str,
    minimum: float,
    minimum_inclusive: bool,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool):
        raise ConfigurationError(f"{name} must be a finite number")
    if isinstance(value, (int, float)):
        try:
            result = float(value)
        except OverflowError as error:
            raise ConfigurationError(f"{name} must be a finite number") from error
    elif isinstance(value, str) and value and value == value.strip():
        try:
            result = float(value)
        except (OverflowError, ValueError) as error:
            raise ConfigurationError(f"{name} must be a finite number") from error
    else:
        raise ConfigurationError(f"{name} must be a finite number")

    if not math.isfinite(result):
        raise ConfigurationError(f"{name} must be finite")
    if minimum_inclusive:
        if result < minimum:
            raise ConfigurationError(f"{name} must be >= {minimum}")
    elif result <= minimum:
        raise ConfigurationError(f"{name} must be > {minimum}")
    if maximum is not None and result > maximum:
        raise ConfigurationError(f"{name} must be <= {maximum}")
    return result


def _server_list(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        servers = tuple(part.strip() for part in value.split(","))
        if not servers or any(not server for server in servers):
            raise ConfigurationError(
                f"{ENV_NTP_SERVERS} must be a comma-separated list of NTP servers"
            )
        return servers
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return tuple(value)
    raise ConfigurationError("NTP servers must be configured as hostnames/IP addresses")


def resolve_launch_settings(
    namespace: argparse.Namespace,
    environment: Mapping[str, str],
) -> ApiLaunchSettings:
    """Resolve CLI > environment > defaults and validate the complete result."""

    defaults = SyncConfig()
    host = validate_api_host(
        _select(namespace.host, environment, ENV_API_HOST, DEFAULT_API_HOST)
    )
    port = validate_api_port(
        _integer(
            _select(namespace.port, environment, ENV_API_PORT, DEFAULT_API_PORT),
            name="API port",
        )
    )
    cycle = _finite_number(
        _select(namespace.cycle, environment, ENV_CYCLE, defaults.cycle),
        name="cycle",
        minimum=0.0,
        minimum_inclusive=False,
        maximum=MAX_CYCLE_SECONDS,
    )
    thresh = _finite_number(
        _select(namespace.thresh, environment, ENV_THRESH, defaults.thresh),
        name="thresh",
        minimum=0.0,
        minimum_inclusive=True,
        maximum=defaults.max_correction,
    )
    servers = _server_list(
        _select(namespace.ntp_servers, environment, ENV_NTP_SERVERS, defaults.servers)
    )
    ntp_port = _integer(
        _select(namespace.ntp_port, environment, ENV_NTP_PORT, defaults.ntp_port),
        name="NTP port",
    )
    quorum = _integer(
        _select(namespace.quorum, environment, ENV_QUORUM, defaults.quorum),
        name="quorum",
    )
    log_file = validate_log_file(
        _select(
            namespace.log_file,
            environment,
            ENV_LOG_FILE,
            DEFAULT_API_LOG_FILE,
        )
    )
    log_level = validate_log_level(
        _select(
            namespace.log_level,
            environment,
            ENV_LOG_LEVEL,
            DEFAULT_API_LOG_LEVEL,
        )
    )

    sync_config = SyncConfig(
        servers=servers,
        ntp_port=ntp_port,
        cycle=cycle,
        thresh=thresh,
        quorum=quorum,
    )
    return ApiLaunchSettings(
        host=host,
        port=port,
        sync_config=sync_config,
        log_file=log_file,
        log_level=log_level,
    )


def _missing_api_dependency(error: ModuleNotFoundError) -> bool:
    missing = (error.name or "").split(".", 1)[0]
    return missing in _OPTIONAL_API_MODULES


def _load_server_runner() -> Callable[..., None]:
    # Delayed import keeps the installed console command capable of explaining
    # a missing optional extra without an import traceback.
    from .api import run_server

    return run_server


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    server_runner: Callable[..., None] | None = None,
) -> int:
    """Run the management server and return a documented process exit code."""

    parser = _parser()
    namespace = parser.parse_args(argv)
    environment = os.environ if environ is None else environ

    try:
        settings = resolve_launch_settings(namespace, environment)
    except ConfigurationError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_INVALID_CONFIGURATION

    run_server = server_runner
    if run_server is None:
        try:
            run_server = _load_server_runner()
        except ModuleNotFoundError as error:
            if not _missing_api_dependency(error):
                print(
                    "error: management API startup failed "
                    "(ModuleNotFoundError).",
                    file=sys.stderr,
                )
                return EXIT_STARTUP_FAILURE
            print(
                "error: management API dependencies are unavailable; install them "
                "with 'python -m pip install \"ntp-anchor[api]\"'.",
                file=sys.stderr,
            )
            return EXIT_MISSING_API_DEPENDENCIES
        except ImportError:
            print(
                "error: management API startup failed (ImportError).",
                file=sys.stderr,
            )
            return EXIT_STARTUP_FAILURE

    try:
        manager = create_sync_manager(settings.sync_config)
        run_server(
            manager,
            host=settings.host,
            port=settings.port,
            workers=1,
            reload=False,
            log_file=settings.log_file,
            log_level=settings.log_level,
        )
    except PortInUseError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_PORT_IN_USE
    except ConfigurationError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_INVALID_CONFIGURATION
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except SystemExit:
        # Uvicorn may use SystemExit for a failed lifespan/startup path.  Keep
        # that from colliding with this CLI's documented missing-extra code.
        print(
            "error: management API startup failed (SystemExit).",
            file=sys.stderr,
        )
        return EXIT_STARTUP_FAILURE
    except Exception as error:
        print(
            "error: management API startup failed "
            f"({type(error).__name__}).",
            file=sys.stderr,
        )
        return EXIT_STARTUP_FAILURE
    return EXIT_OK


__all__ = [
    "ApiLaunchSettings",
    "ENV_API_HOST",
    "ENV_API_PORT",
    "ENV_CYCLE",
    "ENV_LOG_FILE",
    "ENV_LOG_LEVEL",
    "ENV_NTP_PORT",
    "ENV_NTP_SERVERS",
    "ENV_QUORUM",
    "ENV_THRESH",
    "EXIT_INTERRUPTED",
    "EXIT_INVALID_CONFIGURATION",
    "EXIT_MISSING_API_DEPENDENCIES",
    "EXIT_OK",
    "EXIT_PORT_IN_USE",
    "EXIT_STARTUP_FAILURE",
    "main",
    "resolve_launch_settings",
]
