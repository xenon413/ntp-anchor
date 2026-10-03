"""Immutable data contracts shared by the NTP client and sync manager."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import math


def _require_utc(value: datetime | None, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, datetime):
        raise ValueError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be normalized to UTC")


def _require_finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")


class LifecycleState(str, Enum):
    """Lifecycle of the single managed background worker."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


class CheckStatus(str, Enum):
    """Stable outcome values exposed by the manager snapshot."""

    NEVER = "never"
    CHECKING = "checking"
    WITHIN_THRESHOLD = "within_threshold"
    SYNC_SUCCEEDED = "sync_succeeded"
    QUERY_FAILED = "query_failed"
    OPERATION_BUSY = "operation_busy"
    UNSAFE_CORRECTION = "unsafe_correction"
    SYNC_FAILED = "sync_failed"
    VERIFICATION_FAILED = "verification_failed"
    CANCELLED = "cancelled"
    WORKER_FAILED = "worker_failed"


@dataclass(frozen=True, slots=True)
class NTPMeasurement:
    """One validated response from one configured NTP authority."""

    server: str
    offset: float
    delay: float
    root_delay: float
    root_dispersion: float
    stratum: int
    leap: int
    version: int
    mode: int
    poll: int
    precision: int
    reference_id: int
    reference_timestamp: float
    origin_timestamp: float
    receive_timestamp: float
    transmit_timestamp: float
    destination_timestamp: float
    received_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.server, str) or not self.server:
            raise ValueError("server must be a non-empty string")
        for name in (
            "offset",
            "delay",
            "root_delay",
            "root_dispersion",
            "reference_timestamp",
            "origin_timestamp",
            "receive_timestamp",
            "transmit_timestamp",
            "destination_timestamp",
        ):
            _require_finite(getattr(self, name), name)
        if self.delay < 0.0 or self.root_dispersion < 0.0:
            raise ValueError("delay and root_dispersion must be non-negative")
        for name in (
            "stratum",
            "leap",
            "version",
            "mode",
            "poll",
            "precision",
            "reference_id",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
        _require_utc(self.received_at, "received_at")


@dataclass(frozen=True, slots=True)
class NTPRejection:
    """A server response or query rejected while building consensus."""

    server: str
    reason: str
    error_type: str

    def __post_init__(self) -> None:
        if not self.server or not self.reason or not self.error_type:
            raise ValueError("rejection fields must be non-empty strings")
        if not all(
            isinstance(value, str)
            for value in (self.server, self.reason, self.error_type)
        ):
            raise ValueError("rejection fields must be strings")


@dataclass(frozen=True, slots=True)
class NTPQueryResult:
    """Consensus from a quorum of mutually agreeing NTP responses."""

    offset: float
    measurements: tuple[NTPMeasurement, ...]
    rejected: tuple[NTPRejection, ...]
    queried_at: datetime

    def __post_init__(self) -> None:
        # accepts any iter object and converts to tuple
        object.__setattr__(self, "measurements", tuple(self.measurements))
        object.__setattr__(self, "rejected", tuple(self.rejected))
        
        _require_finite(self.offset, "offset")
        if not self.measurements:
            raise ValueError("NTP consensus requires at least one measurement")
        if not all(isinstance(item, NTPMeasurement) for item in self.measurements):
            raise ValueError("measurements must contain NTPMeasurement values")
        if not all(isinstance(item, NTPRejection) for item in self.rejected):
            raise ValueError("rejected must contain NTPRejection values")
        _require_utc(self.queried_at, "queried_at")

    @property
    def servers(self) -> tuple[str, ...]:
        return tuple(measurement.server for measurement in self.measurements)


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Structured successful result from one check/policy/sync operation."""

    status: CheckStatus
    measurement: NTPQueryResult
    verification: NTPQueryResult | None
    checked_at: datetime
    synced_at: datetime | None
    correction_attempted: bool

    def __post_init__(self) -> None:
        if not isinstance(self.status, CheckStatus):
            raise ValueError("status must be a CheckStatus")
        if not isinstance(self.correction_attempted, bool):
            raise ValueError("correction_attempted must be a Boolean")
        _require_utc(self.checked_at, "checked_at")
        _require_utc(self.synced_at, "synced_at")

    @property
    def measured_offset(self) -> float:
        latest = self.verification if self.verification is not None else self.measurement
        return latest.offset


@dataclass(frozen=True, slots=True)
class WorkerSnapshot:
    """Atomic public state returned by ``SyncManager.snapshot()``.

    These seven fields intentionally match the planned ``GET /status``
    response.  Internal errors and counters live in ``WorkerDiagnostics`` and
    cannot accidentally leak through a direct snapshot serialization.
    """

    is_running: bool
    cycle: float
    thresh: float
    last_checked_at: datetime | None
    last_synced_at: datetime | None
    last_check_status: CheckStatus
    last_measured_offset: float | None

    def __post_init__(self) -> None:
        if not isinstance(self.is_running, bool):
            raise ValueError("is_running must be a Boolean")
        if not isinstance(self.last_check_status, CheckStatus):
            raise ValueError("last_check_status must be a CheckStatus")
        _require_finite(self.cycle, "cycle")
        _require_finite(self.thresh, "thresh")
        if self.cycle <= 0.0:
            raise ValueError("cycle must be positive")
        if self.thresh < 0.0:
            raise ValueError("thresh must be non-negative")
        if self.last_measured_offset is not None:
            _require_finite(self.last_measured_offset, "last_measured_offset")
        _require_utc(self.last_checked_at, "last_checked_at")
        _require_utc(self.last_synced_at, "last_synced_at")


@dataclass(frozen=True, slots=True)
class WorkerDiagnostics:
    """Sanitized internal diagnostics, separate from the public API schema."""

    lifecycle: LifecycleState
    consecutive_failures: int
    last_error: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.lifecycle, LifecycleState):
            raise ValueError("lifecycle must be a LifecycleState")
        if (
            isinstance(self.consecutive_failures, bool)
            or not isinstance(self.consecutive_failures, int)
            or self.consecutive_failures < 0
        ):
            raise ValueError("consecutive_failures cannot be negative")


__all__ = [
    "CheckStatus",
    "LifecycleState",
    "NTPMeasurement",
    "NTPQueryResult",
    "NTPRejection",
    "SyncResult",
    "WorkerDiagnostics",
    "WorkerSnapshot",
]
