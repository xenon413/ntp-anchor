"""Cross-platform NTP measurement and managed synchronization."""

from importlib.metadata import PackageNotFoundError, version as _package_version

from . import core
from .core import (
    CheckStatus,
    DEFAULT_NTP_SERVERS,
    LifecycleState,
    NTPMeasurement,
    NTPQueryResult,
    NTPRejection,
    SyncConfig,
    SyncManager,
    SyncResult,
    WorkerSnapshot,
    WorkerDiagnostics,
    check_and_sync,
    check_and_sync_thread,
    configure_default_manager,
    create_sync_manager,
    exceptions,
    get_default_manager,
    get_ntp_time_offset,
    measure_ntp,
    start_time_service,
    sync_time,
)


try:
    __version__ = _package_version("ntp-anchor")
except PackageNotFoundError:
    # A source tree that has not been installed has no distribution metadata.
    # Keep the package importable without duplicating the release version.
    __version__ = "unknown"


__all__ = [
    "CheckStatus",
    "DEFAULT_NTP_SERVERS",
    "LifecycleState",
    "NTPMeasurement",
    "NTPQueryResult",
    "NTPRejection",
    "SyncConfig",
    "SyncManager",
    "SyncResult",
    "WorkerSnapshot",
    "WorkerDiagnostics",
    "__version__",
    "check_and_sync",
    "check_and_sync_thread",
    "configure_default_manager",
    "core",
    "create_sync_manager",
    "exceptions",
    "get_default_manager",
    "get_ntp_time_offset",
    "measure_ntp",
    "start_time_service",
    "sync_time",
]
