from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace
import threading
from unittest.mock import MagicMock

import pytest

import ntp_anchor
import ntp_anchor.core.core_logic as core_logic
from ntp_anchor.core.config import SyncConfig
from ntp_anchor.core.exceptions import WorkerStateError
from ntp_anchor.core.models import (
    CheckStatus,
    NTPMeasurement,
    NTPQueryResult,
    SyncResult,
)


pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_timestamp_reads_fresh_epoch_seconds_without_a_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readings = [1_700_000_000.125, 1_700_000_001.5, 1_699_999_999.75]
    clock = MagicMock(side_effect=readings)
    manager = MagicMock(side_effect=AssertionError("timestamp must not use a manager"))
    monkeypatch.setattr(core_logic, "_system_time", clock)
    monkeypatch.setattr(core_logic, "get_default_manager", manager)
    monkeypatch.setattr(core_logic, "_default_manager", None)

    # All public import paths read on every call, even if the clock moves back.
    assert ntp_anchor.timestamp() == readings[0]
    assert ntp_anchor.core.timestamp() == readings[1]
    assert core_logic.timestamp() == readings[2]
    assert clock.call_count == 3
    manager.assert_not_called()
    assert core_logic._default_manager is None


def _query_result(offset: float = 0.1) -> NTPQueryResult:
    measurement = NTPMeasurement(
        server="time.example.com",
        offset=offset,
        delay=0.01,
        root_delay=0.01,
        root_dispersion=0.01,
        stratum=2,
        leap=0,
        version=3,
        mode=4,
        poll=6,
        precision=-20,
        reference_id=1,
        reference_timestamp=1_700_000_000.0,
        origin_timestamp=1_700_000_001.0,
        receive_timestamp=1_700_000_001.01,
        transmit_timestamp=1_700_000_001.02,
        destination_timestamp=1_700_000_001.03,
        received_at=NOW,
    )
    return NTPQueryResult(
        offset=offset,
        measurements=(measurement,),
        rejected=(),
        queried_at=NOW,
    )


def _sync_result() -> SyncResult:
    measurement = _query_result()
    return SyncResult(
        status=CheckStatus.WITHIN_THRESHOLD,
        measurement=measurement,
        verification=None,
        checked_at=NOW,
        synced_at=None,
        correction_attempted=False,
    )


class FixedClient:
    def __init__(self, result: NTPQueryResult) -> None:
        self.result = result

    def query(self, _config: SyncConfig) -> NTPQueryResult:
        return self.result


def _config(**overrides: object) -> SyncConfig:
    values: dict[str, object] = {
        "servers": ("time.example.com",),
        "quorum": 1,
    }
    values.update(overrides)
    return SyncConfig(**values)  # type: ignore[arg-type]


def test_backend_selection_is_lazy_and_read_only_measurement_stays_portable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = MagicMock()
    factory = MagicMock(return_value=backend)
    monkeypatch.setattr(core_logic, "create_time_sync_backend", factory)
    manager = core_logic.create_sync_manager(
        _config(),
        client=FixedClient(_query_result(0.1)),
        os_name="unsupported-until-needed",
    )

    result = manager.check_once()

    assert result.status is CheckStatus.WITHIN_THRESHOLD
    factory.assert_not_called()

    manager.start_time_service()
    factory.assert_called_once_with(
        "unsupported-until-needed",
        command_timeout=10.0,
    )
    backend.prepare_time_sync.assert_called_once_with(("time.example.com",))


def test_default_manager_is_constructed_once_under_concurrent_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = object()
    factory = MagicMock(return_value=manager)
    monkeypatch.setattr(core_logic, "_default_manager", None)
    monkeypatch.setattr(core_logic, "create_sync_manager", factory)
    callers = 16
    barrier = threading.Barrier(callers)

    def get_manager() -> object:
        barrier.wait()
        return core_logic.get_default_manager()

    with ThreadPoolExecutor(max_workers=callers) as executor:
        managers = list(executor.map(lambda _: get_manager(), range(callers)))

    assert managers == [manager] * callers
    factory.assert_called_once_with()


def test_running_default_manager_cannot_be_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = MagicMock()
    current.snapshot.return_value = SimpleNamespace(is_running=True)
    factory = MagicMock()
    monkeypatch.setattr(core_logic, "_default_manager", current)
    monkeypatch.setattr(core_logic, "create_sync_manager", factory)

    with pytest.raises(WorkerStateError):
        core_logic.configure_default_manager(_config())

    factory.assert_not_called()
    assert core_logic._default_manager is current


def test_compatibility_wrappers_share_one_default_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MagicMock()
    measurement = _query_result()
    result = _sync_result()
    manager.measure_once.return_value = measurement
    manager.check_once.return_value = result
    monkeypatch.setattr(core_logic, "get_default_manager", lambda: manager)

    assert core_logic.measure_ntp() is measurement
    assert core_logic.check_and_sync() is result
    core_logic.start_time_service()
    core_logic.sync_time()

    manager.measure_once.assert_called_once_with()
    manager.check_once.assert_called_once_with()
    manager.start_time_service.assert_called_once_with()
    manager.sync_time.assert_called_once_with()


def test_get_offset_builds_validated_config_and_returns_consensus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    measurement = _query_result(-0.125)
    measure = MagicMock(return_value=measurement)
    monkeypatch.setattr(core_logic, "measure_ntp", measure)

    offset = core_logic.get_ntp_time_offset(
        ("time.example.com",),
        ntp_port=1123,
        timeout=1.5,
        quorum=1,
    )

    assert offset == pytest.approx(-0.125)
    config = measure.call_args.args[0]
    assert config.servers == ("time.example.com",)
    assert config.ntp_port == 1123
    assert config.query_timeout == pytest.approx(1.5)
    assert config.quorum == 1


def test_explicit_manager_cannot_be_mixed_with_per_call_overrides() -> None:
    manager = MagicMock()

    with pytest.raises(ValueError, match="cannot be combined"):
        core_logic.check_and_sync(manager=manager, thresh=0.1)

    manager.check_once.assert_not_called()


def test_per_call_check_overrides_do_not_replace_default_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default = MagicMock()
    default.config = _config(cycle=60.0)
    operation = MagicMock()
    operation.check_once.return_value = _sync_result()
    create = MagicMock(return_value=operation)
    monkeypatch.setattr(core_logic, "get_default_manager", lambda: default)
    monkeypatch.setattr(core_logic, "create_sync_manager", create)

    result = core_logic.check_and_sync(
        ntp_server=("other.example.com",),
        thresh=0.25,
        auto_start=False,
        quorum=1,
        max_correction=2.0,
    )

    assert result is operation.check_once.return_value
    operation_config = create.call_args.args[0]
    assert operation_config.servers == ("other.example.com",)
    assert operation_config.thresh == pytest.approx(0.25)
    assert operation_config.auto_start_service is False
    assert operation_config.quorum == 1
    assert operation_config.max_correction == pytest.approx(2.0)
    assert default.config == _config(cycle=60.0)
