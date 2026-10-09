from collections.abc import Sequence
from datetime import datetime, timezone
import threading

import pytest

from ntp_anchor.core.config import SyncConfig
from ntp_anchor.core.core_logic import create_sync_manager
from ntp_anchor.core.exceptions import (
    ConfigurationError,
    SyncVerificationError,
    TimeServiceUnavailableError,
    UnsafeCorrectionError,
    WorkerStateError,
    WorkerStopTimeoutError,
)
from ntp_anchor.core.manager import SyncManager
from ntp_anchor.core.models import (
    CheckStatus,
    NTPMeasurement,
    NTPQueryResult,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _query_result(
    offset: float,
    *,
    servers: tuple[str, ...] = ("time.example.com",),
    sample_offsets: tuple[float, ...] | None = None,
) -> NTPQueryResult:
    offsets = sample_offsets if sample_offsets is not None else (offset,) * len(servers)
    measurements = tuple(
        NTPMeasurement(
            server=server,
            offset=sample_offset,
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
        for server, sample_offset in zip(servers, offsets, strict=True)
    )
    return NTPQueryResult(
        offset=offset,
        measurements=measurements,
        rejected=(),
        queried_at=NOW,
    )


class QueueClient:
    def __init__(self, *responses: NTPQueryResult | BaseException) -> None:
        self.responses = list(responses)
        self.configs: list[SyncConfig] = []

    def query(self, config: SyncConfig) -> NTPQueryResult:
        self.configs.append(config)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeBackend:
    def __init__(self) -> None:
        self.start_calls = 0
        self.prepare_calls: list[tuple[str, ...]] = []
        self.sync_calls: list[tuple[str, ...]] = []

    def start_time_service(self) -> None:
        self.start_calls += 1

    def prepare_time_sync(self, servers: Sequence[str]) -> None:
        self.start_calls += 1
        self.prepare_calls.append(tuple(servers))

    def sync_time(self, servers: Sequence[str]) -> None:
        self.sync_calls.append(tuple(servers))


def _config(**overrides: object) -> SyncConfig:
    values: dict[str, object] = {
        "servers": ("time.example.com",),
        "quorum": 1,
        "thresh": 0.5,
        "max_correction": 5.0,
        "verification_attempts": 1,
        "verification_interval": 0.0,
        "cycle": 60.0,
        "retry_base_delay": 0.01,
        "retry_max_delay": 0.01,
        "retry_jitter": 0.0,
        "single_flight_timeout": 0.1,
        "stop_timeout": 1.0,
    }
    values.update(overrides)
    return SyncConfig(**values)  # type: ignore[arg-type]


@pytest.mark.unit
def test_within_threshold_does_not_touch_backend() -> None:
    backend = FakeBackend()
    manager = SyncManager(
        _config(),
        client=QueueClient(_query_result(0.5)),
        backend=backend,
        utc_now=lambda: NOW,
    )

    result = manager.check_once()

    assert result.status is CheckStatus.WITHIN_THRESHOLD
    assert result.correction_attempted is False
    assert backend.start_calls == 0
    assert backend.prepare_calls == []
    assert backend.sync_calls == []
    assert manager.snapshot().last_measured_offset == pytest.approx(0.5)


@pytest.mark.unit
def test_sync_uses_configured_servers_and_requires_convergence() -> None:
    backend = FakeBackend()
    client = QueueClient(_query_result(-1.0), _query_result(-0.1))
    manager = SyncManager(
        _config(),
        client=client,
        backend=backend,
        utc_now=lambda: NOW,
    )

    result = manager.check_once()

    assert result.status is CheckStatus.SYNC_SUCCEEDED
    assert result.correction_attempted is True
    assert result.measured_offset == pytest.approx(-0.1)
    assert backend.start_calls == 1
    assert backend.prepare_calls == [("time.example.com",)]
    assert backend.sync_calls == [("time.example.com",)]
    assert manager.snapshot().last_synced_at == NOW


@pytest.mark.unit
def test_unsafe_offset_never_invokes_backend() -> None:
    backend = FakeBackend()
    manager = SyncManager(
        _config(),
        client=QueueClient(_query_result(5.01)),
        backend=backend,
        utc_now=lambda: NOW,
    )

    with pytest.raises(UnsafeCorrectionError):
        manager.check_once()

    assert backend.start_calls == 0
    assert backend.prepare_calls == []
    assert backend.sync_calls == []
    assert manager.snapshot().last_check_status is CheckStatus.UNSAFE_CORRECTION


@pytest.mark.unit
def test_unbounded_policy_preserves_checks_but_allows_large_correction() -> None:
    backend = FakeBackend()
    manager = SyncManager(
        _config(max_correction=None),
        client=QueueClient(_query_result(500.0), _query_result(0.1)),
        backend=backend,
        utc_now=lambda: NOW,
    )

    result = manager.check_once()

    assert result.status is CheckStatus.SYNC_SUCCEEDED
    assert backend.prepare_calls == [("time.example.com",)]
    assert backend.sync_calls == [("time.example.com",)]


@pytest.mark.unit
def test_only_consensus_inliers_reach_the_platform_backend() -> None:
    backend = FakeBackend()
    trusted = ("a.example.com", "b.example.com")
    config = _config(
        servers=(*trusted, "rejected.example.com"),
        quorum=2,
    )
    client = QueueClient(
        _query_result(1.0, servers=trusted),
        _query_result(0.1, servers=trusted),
    )
    manager = SyncManager(config, client=client, backend=backend, utc_now=lambda: NOW)

    manager.check_once()

    assert backend.prepare_calls == [trusted]
    assert backend.sync_calls == [trusted]


@pytest.mark.unit
def test_individually_unsafe_inliers_cannot_satisfy_correction_quorum() -> None:
    backend = FakeBackend()
    servers = ("a.example.com", "b.example.com")
    manager = SyncManager(
        _config(servers=servers, quorum=2),
        client=QueueClient(
            _query_result(
                4.95,
                servers=servers,
                sample_offsets=(4.9, 5.1),
            )
        ),
        backend=backend,
        utc_now=lambda: NOW,
    )

    with pytest.raises(UnsafeCorrectionError):
        manager.check_once()

    assert backend.prepare_calls == []
    assert backend.sync_calls == []


@pytest.mark.unit
def test_nonstandard_ntp_port_is_measurement_only() -> None:
    backend = FakeBackend()
    manager = SyncManager(
        _config(ntp_port=1123),
        client=QueueClient(_query_result(1.0)),
        backend=backend,
        utc_now=lambda: NOW,
    )

    with pytest.raises(TimeServiceUnavailableError):
        manager.check_once()

    assert manager.snapshot().last_check_status is CheckStatus.SYNC_FAILED
    assert backend.prepare_calls == []
    assert backend.sync_calls == []


@pytest.mark.unit
def test_nonconverged_sync_does_not_set_last_synced_at() -> None:
    manager = SyncManager(
        _config(),
        client=QueueClient(_query_result(1.0), _query_result(0.75)),
        backend=FakeBackend(),
        utc_now=lambda: NOW,
    )

    with pytest.raises(SyncVerificationError):
        manager.check_once()

    snapshot = manager.snapshot()
    assert snapshot.last_check_status is CheckStatus.VERIFICATION_FAILED
    assert snapshot.last_measured_offset == pytest.approx(0.75)
    assert snapshot.last_synced_at is None


@pytest.mark.unit
def test_invalid_dynamic_updates_leave_state_unchanged() -> None:
    manager = SyncManager(_config(), client=QueueClient(), backend=FakeBackend())
    original = manager.snapshot()

    with pytest.raises(ConfigurationError):
        manager.set_cycle(0.0)
    with pytest.raises(ConfigurationError):
        manager.set_thresh(6.0)
    with pytest.raises(ConfigurationError):
        manager.set_max_correction(0.25)

    assert manager.snapshot() == original
    assert manager.config.max_correction == pytest.approx(5.0)


@pytest.mark.unit
def test_maximum_correction_update_is_atomic_and_applies_to_next_check() -> None:
    manager = SyncManager(_config(), client=QueueClient(), backend=FakeBackend())

    disabled = manager.set_max_correction(None)
    assert disabled.is_running is False
    assert manager.config.max_correction is None

    manager.set_thresh(10.0)
    enabled = manager.set_max_correction(20.0)
    assert enabled.is_running is False
    assert manager.config.max_correction == pytest.approx(20.0)


@pytest.mark.unit
def test_start_and_stop_are_idempotent_and_cycle_update_keeps_thread() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingClient:
        def query(self, config: SyncConfig) -> NTPQueryResult:
            entered.set()
            assert release.wait(1.0)
            return _query_result(0.1)

    manager = SyncManager(_config(), client=BlockingClient(), backend=FakeBackend())

    first = manager.start()
    assert first.is_running is True
    assert entered.wait(1.0)
    thread = manager.thread
    second = manager.start()
    assert second.is_running is True
    assert manager.thread is thread

    changed = manager.set_cycle(30.0)
    assert changed.cycle == 30.0
    assert manager.thread is thread

    release.set()
    stopped = manager.stop()
    assert stopped.is_running is False
    assert manager.stop().is_running is False


@pytest.mark.unit
def test_thread_factory_failure_restores_a_nonrunning_failed_state() -> None:
    def broken_thread_factory(**_: object) -> threading.Thread:
        raise RuntimeError("cannot create thread")

    manager = SyncManager(
        _config(),
        client=QueueClient(),
        backend=FakeBackend(),
        thread_factory=broken_thread_factory,
    )

    with pytest.raises(WorkerStateError):
        manager.start()

    assert manager.snapshot().is_running is False
    assert manager.snapshot().last_check_status is CheckStatus.WORKER_FAILED


@pytest.mark.unit
def test_concurrent_starts_create_exactly_one_worker() -> None:
    entered = threading.Event()
    release = threading.Event()
    barrier = threading.Barrier(9)

    class BlockingClient:
        def query(self, config: SyncConfig) -> NTPQueryResult:
            entered.set()
            assert release.wait(1.0)
            return _query_result(0.1)

    manager = SyncManager(_config(), client=BlockingClient(), backend=FakeBackend())
    returned_threads: list[threading.Thread | None] = []

    def start_manager() -> None:
        barrier.wait()
        manager.start()
        returned_threads.append(manager.thread)

    callers = [threading.Thread(target=start_manager) for _ in range(8)]
    for caller in callers:
        caller.start()
    barrier.wait()
    for caller in callers:
        caller.join(1.0)

    assert entered.wait(1.0)
    assert len(returned_threads) == 8
    assert len({id(thread) for thread in returned_threads}) == 1

    release.set()
    manager.stop()


@pytest.mark.unit
def test_stop_timeout_keeps_running_state_until_worker_exits() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingClient:
        def query(self, config: SyncConfig) -> NTPQueryResult:
            entered.set()
            assert release.wait(1.0)
            return _query_result(0.1)

    manager = SyncManager(_config(), client=BlockingClient(), backend=FakeBackend())
    manager.start()
    assert entered.wait(1.0)

    with pytest.raises(WorkerStopTimeoutError) as caught:
        manager.stop(timeout=0.01)

    assert caught.value.timeout == pytest.approx(0.01)
    assert manager.snapshot().is_running is True
    release.set()
    assert manager.stop().is_running is False


@pytest.mark.unit
def test_process_wide_single_flight_lock_applies_across_managers() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingClient:
        def query(self, config: SyncConfig) -> NTPQueryResult:
            entered.set()
            assert release.wait(1.0)
            return _query_result(0.1)

    first = SyncManager(
        _config(single_flight_timeout=1.0),
        client=BlockingClient(),
        backend=FakeBackend(),
    )
    second = SyncManager(
        _config(single_flight_timeout=0.01),
        client=QueueClient(_query_result(0.1)),
        backend=FakeBackend(),
    )
    failure: list[BaseException] = []

    def run_first() -> None:
        try:
            first.check_once()
        except BaseException as error:  # pragma: no cover - assertion aid
            failure.append(error)

    worker = threading.Thread(target=run_first)
    worker.start()
    assert entered.wait(1.0)

    with pytest.raises(WorkerStateError):
        second.check_once()

    assert second.snapshot().last_check_status is CheckStatus.OPERATION_BUSY
    assert second.diagnostics().consecutive_failures == 1

    release.set()
    worker.join(1.0)
    assert not worker.is_alive()
    assert failure == []


@pytest.mark.unit
def test_unsupported_os_can_still_run_read_only_measurement() -> None:
    manager = create_sync_manager(
        _config(),
        client=QueueClient(_query_result(0.1)),
        os_name="unsupported-test-os",
    )

    result = manager.check_once()

    assert result.status is CheckStatus.WITHIN_THRESHOLD


@pytest.mark.unit
def test_threshold_update_during_query_applies_to_the_next_check() -> None:
    backend = FakeBackend()

    class UpdatingClient:
        manager: SyncManager

        def __init__(self) -> None:
            self.calls = 0

        def query(self, config: SyncConfig) -> NTPQueryResult:
            self.calls += 1
            if self.calls == 1:
                assert config.thresh == pytest.approx(0.5)
                self.manager.set_thresh(2.0)
                return _query_result(1.0)
            return _query_result(0.1)

    client = UpdatingClient()
    manager = SyncManager(
        _config(),
        client=client,
        backend=backend,
        utc_now=lambda: NOW,
    )
    client.manager = manager

    result = manager.check_once()

    assert result.status is CheckStatus.SYNC_SUCCEEDED
    assert backend.sync_calls == [("time.example.com",)]
    assert manager.snapshot().thresh == pytest.approx(2.0)


@pytest.mark.unit
def test_snapshots_remain_internally_consistent_during_concurrent_checks() -> None:
    started = threading.Event()
    finished = threading.Event()
    offsets = (0.1, 0.2)

    class AlternatingClient:
        def __init__(self) -> None:
            self.calls = 0

        def query(self, _config: SyncConfig) -> NTPQueryResult:
            started.set()
            offset = offsets[self.calls % len(offsets)]
            self.calls += 1
            return _query_result(offset)

    manager = SyncManager(
        _config(),
        client=AlternatingClient(),
        backend=FakeBackend(),
        utc_now=lambda: NOW,
    )
    observed = []

    def write_checks() -> None:
        try:
            for _ in range(200):
                manager.check_once()
        finally:
            finished.set()

    writer = threading.Thread(target=write_checks)
    writer.start()
    assert started.wait(1.0)
    while not finished.is_set():
        observed.append(manager.snapshot())
    writer.join(1.0)
    assert not writer.is_alive()
    observed.append(manager.snapshot())

    for snapshot in observed:
        if snapshot.last_check_status is CheckStatus.NEVER:
            assert snapshot.last_checked_at is None
            assert snapshot.last_measured_offset is None
        elif snapshot.last_check_status is CheckStatus.CHECKING:
            assert snapshot.last_measured_offset is None
        else:
            assert snapshot.last_check_status is CheckStatus.WITHIN_THRESHOLD
            assert snapshot.last_checked_at == NOW
            assert snapshot.last_measured_offset in offsets


@pytest.mark.unit
def test_cycle_change_recomputes_wait_without_replacing_worker() -> None:
    clock = {"now": 0.0}
    wait_started = threading.Event()
    second_check = threading.Event()

    class CountingClient:
        def __init__(self) -> None:
            self.calls = 0

        def query(self, _config: SyncConfig) -> NTPQueryResult:
            self.calls += 1
            if self.calls == 2:
                second_check.set()
            return _query_result(0.1)

    client = CountingClient()
    manager = SyncManager(
        _config(cycle=300.0),
        client=client,
        backend=FakeBackend(),
        monotonic=lambda: clock["now"],
    )
    original_wait = manager._wait_for_cycle

    def observed_wait(completed_at: float, stop_event: threading.Event) -> None:
        wait_started.set()
        original_wait(completed_at, stop_event)

    manager._wait_for_cycle = observed_wait  # type: ignore[method-assign]
    manager.start()
    thread = manager.thread
    assert wait_started.wait(1.0)

    clock["now"] = 10.0
    manager.set_cycle(1.0)

    assert second_check.wait(1.0)
    assert manager.thread is thread
    assert client.calls >= 2
    assert manager.stop().is_running is False


@pytest.mark.unit
def test_failure_path_enters_backoff_and_stop_wakes_it_immediately() -> None:
    backoff_entered = threading.Event()

    class FailingClient:
        def __init__(self) -> None:
            self.calls = 0

        def query(self, _config: SyncConfig) -> NTPQueryResult:
            self.calls += 1
            raise TimeServiceUnavailableError("test failure")

    client = FailingClient()
    manager = SyncManager(
        _config(retry_base_delay=30.0, retry_max_delay=30.0),
        client=client,
        backend=FakeBackend(),
    )
    original_backoff = manager._wait_for_backoff

    def observed_backoff(stop_event: threading.Event) -> None:
        backoff_entered.set()
        original_backoff(stop_event)

    manager._wait_for_backoff = observed_backoff  # type: ignore[method-assign]
    manager.start()
    assert backoff_entered.wait(1.0)

    assert manager.stop(timeout=1.0).is_running is False
    assert client.calls == 1
