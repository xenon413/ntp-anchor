"""Core ntp-anchor public API."""

from . import exceptions
from .config import DEFAULT_NTP_SERVERS, SyncConfig
from .core_logic import (
    check_and_sync,
    check_and_sync_thread,
    configure_default_manager,
    create_sync_manager,
    get_default_manager,
    get_ntp_time_offset,
    measure_ntp,
    start_time_service,
    sync_time,
)
from .manager import SyncManager
from .models import (
    CheckStatus,
    LifecycleState,
    NTPMeasurement,
    NTPQueryResult,
    NTPRejection,
    SyncResult,
    WorkerDiagnostics,
    WorkerSnapshot,
)


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
    "WorkerDiagnostics",
    "WorkerSnapshot",
    "check_and_sync",
    "check_and_sync_thread",
    "configure_default_manager",
    "create_sync_manager",
    "exceptions",
    "get_default_manager",
    "get_ntp_time_offset",
    "measure_ntp",
    "start_time_service",
    "sync_time",
]
