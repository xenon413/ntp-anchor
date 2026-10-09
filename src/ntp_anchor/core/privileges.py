"""Immutable results for host clock-correction privilege detection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TimeSyncPrivilegeStatus(str, Enum):
    """Whether this process may perform platform clock-correction operations."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TimeSyncPrivilegeCheck:
    """One cached, non-mutating platform privilege check."""

    status: TimeSyncPrivilegeStatus
    error_type: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, TimeSyncPrivilegeStatus):
            raise ValueError("status must be a TimeSyncPrivilegeStatus")
        if self.status is TimeSyncPrivilegeStatus.UNKNOWN:
            if not isinstance(self.error_type, str) or not self.error_type:
                raise ValueError("unknown privilege status requires an error type")
        elif self.error_type is not None:
            raise ValueError("known privilege status cannot include an error type")

    @property
    def correction_available(self) -> bool:
        """Return whether privileged correction endpoints may proceed."""

        return self.status is TimeSyncPrivilegeStatus.AVAILABLE


__all__ = ["TimeSyncPrivilegeCheck", "TimeSyncPrivilegeStatus"]
