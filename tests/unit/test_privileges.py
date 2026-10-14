from __future__ import annotations

import pytest

from ntp_anchor.core.backends import factory as backend_factory
from ntp_anchor.core.privileges import (
    TimeSyncPrivilegeCheck,
    TimeSyncPrivilegeStatus,
)


pytestmark = pytest.mark.unit


def test_privilege_check_exposes_only_available_as_correction_capable() -> None:
    assert TimeSyncPrivilegeCheck(
        TimeSyncPrivilegeStatus.AVAILABLE
    ).correction_available
    assert not TimeSyncPrivilegeCheck(
        TimeSyncPrivilegeStatus.UNAVAILABLE
    ).correction_available
    assert not TimeSyncPrivilegeCheck(
        TimeSyncPrivilegeStatus.UNKNOWN,
        error_type="OSError",
    ).correction_available


def test_unknown_privilege_check_requires_a_sanitized_error_type() -> None:
    with pytest.raises(ValueError):
        TimeSyncPrivilegeCheck(TimeSyncPrivilegeStatus.UNKNOWN)
    with pytest.raises(ValueError):
        TimeSyncPrivilegeCheck(
            TimeSyncPrivilegeStatus.AVAILABLE,
            error_type="OSError",
        )


def test_unsupported_platform_is_unknown_instead_of_raising() -> None:
    result = backend_factory.detect_time_sync_privilege("unsupported-test-os")

    assert result.status is TimeSyncPrivilegeStatus.UNKNOWN
    assert result.error_type == "UnsupportedOSError"


def test_platform_detection_failure_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> str:
        raise RuntimeError("private platform detail")

    monkeypatch.setattr(backend_factory.platform, "system", fail)

    result = backend_factory.detect_time_sync_privilege()

    assert result.status is TimeSyncPrivilegeStatus.UNKNOWN
    assert result.error_type == "RuntimeError"
