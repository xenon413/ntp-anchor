from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import threading
from typing import Any

from fastapi.testclient import TestClient
import pytest

from ntp_anchor.api import MAX_CYCLE_SECONDS, create_app
from ntp_anchor.core.config import SyncConfig
from ntp_anchor.core.exceptions import (
    ConfigurationError,
    TimeServiceUnavailableError,
    WorkerStateError,
    WorkerStopTimeoutError,
)
from ntp_anchor.core.manager import SyncManager
from ntp_anchor.core.models import (
    CheckStatus,
    NTPMeasurement,
    NTPQueryResult,
    WorkerSnapshot,
)


pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def _snapshot(**overrides: object) -> WorkerSnapshot:
    values: dict[str, object] = {
        "is_running": False,
        "cycle": 300.0,
        "thresh": 0.5,
        "last_checked_at": None,
        "last_synced_at": None,
        "last_check_status": CheckStatus.NEVER,
        "last_measured_offset": None,
    }
    values.update(overrides)
    return WorkerSnapshot(**values)  # type: ignore[arg-type]


class FakeManager:
    """Thread-safe route double that tracks real lifecycle transitions."""

    def __init__(self, *, max_correction: float = 5.0) -> None:
        self._config = SyncConfig(
            servers=("time.example.com",),
            quorum=1,
            max_correction=max_correction,
        )
        self._snapshot = _snapshot(
            cycle=self._config.cycle,
            thresh=self._config.thresh,
        )
        self._lock = threading.RLock()
        self.snapshot_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.set_cycle_calls = 0
        self.set_thresh_calls = 0
        self.worker_starts = 0
        self.worker_stops = 0
        self.start_error: BaseException | None = None
        self.stop_error: BaseException | None = None
        self.set_cycle_error: BaseException | None = None
        self.set_thresh_error: BaseException | None = None
        self.stop_entered: threading.Event | None = None
        self.stop_release: threading.Event | None = None

    @property
    def config(self) -> SyncConfig:
        with self._lock:
            return self._config

    def snapshot(self) -> WorkerSnapshot:
        with self._lock:
            self.snapshot_calls += 1
            return self._snapshot

    def publish(self, snapshot: WorkerSnapshot) -> None:
        with self._lock:
            self._snapshot = snapshot

    def start(self) -> WorkerSnapshot:
        with self._lock:
            self.start_calls += 1
            if self.start_error is not None:
                raise self.start_error
            if not self._snapshot.is_running:
                self.worker_starts += 1
                self._snapshot = replace(self._snapshot, is_running=True)
            return self._snapshot

    def stop(self, timeout: float | None = None) -> WorkerSnapshot:
        del timeout
        entered = self.stop_entered
        release = self.stop_release
        if entered is not None:
            entered.set()
        if release is not None and not release.wait(2.0):
            raise AssertionError("test did not release the fake stop operation")
        with self._lock:
            self.stop_calls += 1
            if self.stop_error is not None:
                raise self.stop_error
            if self._snapshot.is_running:
                self.worker_stops += 1
                self._snapshot = replace(self._snapshot, is_running=False)
            return self._snapshot

    def set_cycle(self, seconds: float) -> WorkerSnapshot:
        with self._lock:
            self.set_cycle_calls += 1
            if self.set_cycle_error is not None:
                raise self.set_cycle_error
            self._config = self._config.with_cycle(seconds)
            self._snapshot = replace(self._snapshot, cycle=seconds)
            return self._snapshot

    def set_thresh(self, seconds: float) -> WorkerSnapshot:
        with self._lock:
            self.set_thresh_calls += 1
            if self.set_thresh_error is not None:
                raise self.set_thresh_error
            self._config = self._config.with_thresh(seconds)
            self._snapshot = replace(self._snapshot, thresh=seconds)
            return self._snapshot


def _client(
    manager: FakeManager,
    shutdown_callback=lambda: None,
    **app_options: Any,
) -> TestClient:
    app = create_app(  # type: ignore[arg-type]
        manager,
        shutdown_callback,
        **app_options,
    )
    return TestClient(app, raise_server_exceptions=False)


def test_ping_is_exact_and_side_effect_free() -> None:
    manager = FakeManager()
    client = _client(manager)

    response = client.get("/ping")

    assert response.status_code == 200
    assert response.json() == {"status": "pong"}
    assert manager.snapshot_calls == 0
    assert manager.start_calls == 0
    assert manager.stop_calls == 0


def test_help_matches_every_management_route_and_constraint() -> None:
    manager = FakeManager(max_correction=7.5)
    app = create_app(manager, lambda: None)  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.get("/help")

    assert response.status_code == 200
    routes = response.json()["routes"]
    expected = [
        ("/ping", "GET"),
        ("/help", "GET"),
        ("/start", "POST"),
        ("/end", "POST"),
        ("/close", "POST"),
        ("/status", "GET"),
        ("/set-cycle", "POST"),
        ("/set-thresh", "POST"),
    ]
    assert [(item["path"], item["method"]) for item in routes] == expected
    assert all(item["description"] for item in routes)

    by_path = {item["path"]: item for item in routes}
    assert by_path["/set-cycle"]["parameters"] == [
        {
            "name": "cycle",
            "location": "body",
            "required": True,
            "type": "number",
            "constraints": {
                "finite": True,
                "exclusive_minimum": 0.0,
                "maximum": MAX_CYCLE_SECONDS,
            },
        }
    ]
    assert by_path["/set-thresh"]["parameters"][0]["constraints"] == {
        "finite": True,
        "minimum": 0.0,
        "maximum": 7.5,
    }
    assert all(
        item["parameters"] == []
        for path, item in by_path.items()
        if path not in {"/set-cycle", "/set-thresh"}
    )

    actual = {
        (route.path, method)
        for route in app.routes
        for method in getattr(route, "methods", set())
        if route.path in by_path
    }
    assert actual == set(expected)


def test_openapi_threshold_bound_matches_help_and_manager_policy() -> None:
    manager = FakeManager(max_correction=3.25)
    client = _client(manager)

    schema = client.get("/openapi.json").json()

    threshold = schema["components"]["schemas"]["ThreshRequest"]["properties"][
        "thresh"
    ]
    assert threshold["maximum"] == pytest.approx(3.25)


def test_status_returns_exact_public_snapshot() -> None:
    manager = FakeManager()
    manager.publish(
        _snapshot(
            is_running=True,
            cycle=60.0,
            thresh=0.25,
            last_checked_at=NOW,
            last_synced_at=NOW,
            last_check_status=CheckStatus.SYNC_SUCCEEDED,
            last_measured_offset=-0.01,
        )
    )

    response = _client(manager).get("/status")

    assert response.status_code == 200
    assert response.json() == {
        "is_running": True,
        "cycle": 60.0,
        "thresh": 0.25,
        "last_checked_at": "2026-01-02T03:04:05Z",
        "last_synced_at": "2026-01-02T03:04:05Z",
        "last_check_status": "sync_succeeded",
        "last_measured_offset": -0.01,
    }


@pytest.mark.parametrize("status", list(CheckStatus))
def test_status_accepts_every_documented_manager_state(status: CheckStatus) -> None:
    manager = FakeManager()
    manager.publish(_snapshot(last_check_status=status))

    response = _client(manager).get("/status")

    assert response.status_code == 200
    assert response.json()["last_check_status"] == status.value


def test_start_and_end_are_idempotent() -> None:
    manager = FakeManager()
    client = _client(manager)

    assert client.post("/start").json() == {"is_running": True}
    assert client.post("/start").json() == {"is_running": True}
    assert manager.worker_starts == 1

    assert client.post("/end").json() == {"is_running": False}
    assert client.post("/end").json() == {"is_running": False}
    assert manager.worker_stops == 1


def test_simultaneous_starts_create_one_worker() -> None:
    manager = FakeManager()
    client = _client(manager)
    callers = 12
    barrier = threading.Barrier(callers)

    def start() -> tuple[int, dict[str, bool]]:
        barrier.wait()
        response = client.post("/start")
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=callers) as executor:
        results = list(executor.map(lambda _: start(), range(callers)))

    assert results == [(200, {"is_running": True})] * callers
    assert manager.worker_starts == 1


def test_simultaneous_ends_stop_one_worker() -> None:
    manager = FakeManager()
    manager.start()
    client = _client(manager)
    callers = 12
    barrier = threading.Barrier(callers)

    def end() -> tuple[int, dict[str, bool]]:
        barrier.wait()
        response = client.post("/end")
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=callers) as executor:
        results = list(executor.map(lambda _: end(), range(callers)))

    assert results == [(200, {"is_running": False})] * callers
    assert manager.worker_stops == 1


def test_start_failure_is_unavailable_and_never_claims_running() -> None:
    manager = FakeManager()
    manager.start_error = WorkerStateError("thread secret")

    response = _client(manager).post("/start")

    assert response.status_code == 503
    assert response.json() == {
        "detail": "The background sync worker could not be started."
    }
    assert manager.snapshot().is_running is False
    assert "secret" not in response.text


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (WorkerStopTimeoutError(0.1), 504),
        (TimeServiceUnavailableError("provider secret"), 503),
    ],
)
def test_end_maps_bounded_operational_failures(
    error: BaseException,
    status_code: int,
) -> None:
    manager = FakeManager()
    manager.start()
    manager.stop_error = error

    response = _client(manager).post("/end")

    assert response.status_code == status_code
    assert manager.snapshot().is_running is True
    assert "secret" not in response.text


def test_close_stops_before_response_and_signals_after_body() -> None:
    manager = FakeManager()
    manager.start()
    response_complete = threading.Event()
    shutdown_calls = 0

    def shutdown() -> None:
        nonlocal shutdown_calls
        assert response_complete.is_set()
        assert manager.snapshot().is_running is False
        shutdown_calls += 1

    app = create_app(manager, shutdown)  # type: ignore[arg-type]

    class ObserveFinalBody:
        def __init__(self, wrapped: Any) -> None:
            self.wrapped = wrapped

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            async def observed_send(message: dict[str, Any]) -> None:
                await send(message)
                if (
                    message["type"] == "http.response.body"
                    and not message.get("more_body", False)
                ):
                    response_complete.set()

            await self.wrapped(scope, receive, observed_send)

    response = TestClient(ObserveFinalBody(app)).post("/close")

    assert response.status_code == 200
    assert response.json() == {"is_running": False, "is_closing": True}
    assert shutdown_calls == 1
    assert manager.worker_stops == 1


def test_repeated_close_signals_shutdown_at_most_once() -> None:
    manager = FakeManager()
    manager.start()
    callback_lock = threading.Lock()
    callback_calls = 0

    def shutdown() -> None:
        nonlocal callback_calls
        with callback_lock:
            callback_calls += 1

    client = _client(manager, shutdown)
    callers = 10
    barrier = threading.Barrier(callers)

    def close() -> tuple[int, dict[str, bool]]:
        barrier.wait()
        response = client.post("/close")
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=callers) as executor:
        results = list(executor.map(lambda _: close(), range(callers)))

    assert results == [
        (200, {"is_running": False, "is_closing": True})
    ] * callers
    assert callback_calls == 1
    assert manager.worker_stops == 1


def test_close_authorization_failure_has_no_side_effects() -> None:
    manager = FakeManager()
    manager.start()
    callback_called = False

    def shutdown() -> None:
        nonlocal callback_called
        callback_called = True

    client = _client(
        manager,
        shutdown,
        close_authorizer=lambda _request: False,
    )

    response = client.post("/close")

    assert response.status_code == 403
    assert manager.snapshot().is_running is True
    assert callback_called is False


def test_close_failure_does_not_signal_shutdown_or_lock_out_retry() -> None:
    manager = FakeManager()
    manager.start()
    manager.stop_error = WorkerStopTimeoutError(0.1)
    callback_called = False

    def shutdown() -> None:
        nonlocal callback_called
        callback_called = True

    client = _client(manager, shutdown)

    response = client.post("/close")

    assert response.status_code == 504
    assert callback_called is False
    manager.stop_error = None
    assert client.post("/end").status_code == 200
    assert client.post("/start").status_code == 200


def test_start_is_rejected_after_close_transition() -> None:
    manager = FakeManager()
    client = _client(manager)

    assert client.post("/close").status_code == 200
    response = client.post("/start")

    assert response.status_code == 409
    assert response.json() == {
        "detail": "The requested lifecycle operation conflicts with shutdown."
    }


def test_lifespan_cleanup_stops_a_running_worker() -> None:
    manager = FakeManager()
    app = create_app(manager, lambda: None)  # type: ignore[arg-type]

    with TestClient(app) as client:
        assert client.post("/start").status_code == 200
        assert manager.snapshot().is_running is True

    assert manager.snapshot().is_running is False
    assert manager.worker_stops == 1


def test_lifespan_cleanup_failure_is_not_silently_suppressed() -> None:
    manager = FakeManager()
    manager.start()
    manager.stop_error = WorkerStopTimeoutError(0.1)
    app = create_app(manager, lambda: None)  # type: ignore[arg-type]

    with pytest.raises(WorkerStopTimeoutError):
        with TestClient(app) as client:
            assert client.get("/ping").status_code == 200

    assert manager.snapshot().is_running is True


@pytest.mark.parametrize("initially_running", [False, True])
def test_cycle_update_preserves_lifecycle(initially_running: bool) -> None:
    manager = FakeManager()
    if initially_running:
        manager.start()

    response = _client(manager).post("/set-cycle", json={"cycle": 60})

    assert response.status_code == 200
    assert response.json() == {
        "cycle": 60.0,
        "is_running": initially_running,
    }
    assert manager.snapshot().is_running is initially_running


@pytest.mark.parametrize("cycle", [0.001, MAX_CYCLE_SECONDS])
def test_cycle_update_accepts_inclusive_valid_boundaries(cycle: float) -> None:
    manager = FakeManager()

    response = _client(manager).post("/set-cycle", json={"cycle": cycle})

    assert response.status_code == 200
    assert response.json() == {"cycle": cycle, "is_running": False}


@pytest.mark.parametrize("initially_running", [False, True])
def test_threshold_update_preserves_lifecycle(initially_running: bool) -> None:
    manager = FakeManager()
    if initially_running:
        manager.start()

    response = _client(manager).post("/set-thresh", json={"thresh": 0.25})

    assert response.status_code == 200
    assert response.json() == {
        "thresh": 0.25,
        "is_running": initially_running,
    }
    assert manager.snapshot().is_running is initially_running


@pytest.mark.parametrize("thresh", [0.0, 5.0])
def test_threshold_update_accepts_inclusive_boundaries(thresh: float) -> None:
    manager = FakeManager(max_correction=5.0)

    response = _client(manager).post("/set-thresh", json={"thresh": thresh})

    assert response.status_code == 200
    assert response.json() == {"thresh": thresh, "is_running": False}


@pytest.mark.parametrize("path", ["/set-cycle", "/set-thresh"])
def test_malformed_json_is_sanitized_422(path: str) -> None:
    response = _client(FakeManager()).post(
        path,
        content="not-json-private-value",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert "not-json-private-value" not in response.text


@pytest.mark.parametrize(
    "body",
    [
        "{}",
        '{"cycle": null}',
        '{"cycle": true}',
        '{"cycle": "1"}',
        '{"cycle": 0}',
        '{"cycle": -1}',
        '{"cycle": NaN}',
        '{"cycle": Infinity}',
        '{"cycle": -Infinity}',
        f'{{"cycle": {MAX_CYCLE_SECONDS + 1}}}',
        '{"cycle": 1, "unexpected": 2}',
    ],
)
def test_invalid_cycle_requests_are_422_and_leave_state_unchanged(body: str) -> None:
    manager = FakeManager()
    original = manager.snapshot()

    response = _client(manager).post(
        "/set-cycle",
        content=body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert manager.snapshot() == original
    assert manager.set_cycle_calls == 0
    assert body not in response.text


@pytest.mark.parametrize(
    "body",
    [
        "{}",
        '{"thresh": null}',
        '{"thresh": true}',
        '{"thresh": "0.1"}',
        '{"thresh": -0.1}',
        '{"thresh": NaN}',
        '{"thresh": Infinity}',
        '{"thresh": -Infinity}',
        '{"thresh": 5.01}',
        '{"thresh": 0.1, "unexpected": 2}',
    ],
)
def test_invalid_threshold_requests_are_422_and_leave_state_unchanged(
    body: str,
) -> None:
    manager = FakeManager(max_correction=5.0)
    original = manager.snapshot()

    response = _client(manager).post(
        "/set-thresh",
        content=body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert manager.snapshot() == original
    assert manager.set_thresh_calls == 0
    assert body not in response.text


def test_setter_configuration_failure_is_opaque() -> None:
    manager = FakeManager()
    manager.set_cycle_error = ConfigurationError("private policy detail")

    response = _client(manager).post("/set-cycle", json={"cycle": 60.0})

    assert response.status_code == 422
    assert response.json() == {"detail": "The requested configuration is invalid."}
    assert "private" not in response.text


def test_unexpected_failure_returns_incident_without_sensitive_detail() -> None:
    manager = FakeManager()
    manager.set_cycle_error = RuntimeError(
        r"secret stderr C:\\sensitive\\provider.exe --token=abc"
    )

    response = _client(manager).post("/set-cycle", json={"cycle": 60.0})

    assert response.status_code == 500
    payload = response.json()
    assert payload["detail"] == "Internal server error."
    assert len(payload["incident_id"]) == 32
    assert "secret" not in response.text
    assert "provider.exe" not in response.text


def test_ping_remains_responsive_while_stop_is_blocked() -> None:
    manager = FakeManager()
    manager.start()
    manager.stop_entered = threading.Event()
    manager.stop_release = threading.Event()
    client = _client(manager)

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending_stop = executor.submit(client.post, "/end")
        assert manager.stop_entered.wait(1.0)

        ping = client.get("/ping")
        assert ping.status_code == 200
        assert ping.json() == {"status": "pong"}

        manager.stop_release.set()
        assert pending_stop.result(timeout=2.0).status_code == 200


def _query_result(offset: float) -> NTPQueryResult:
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


def test_ping_remains_responsive_while_real_manager_query_is_blocked() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingClient:
        def query(self, _config: SyncConfig) -> NTPQueryResult:
            entered.set()
            if not release.wait(2.0):
                raise AssertionError("test did not release the query")
            return _query_result(0.1)

    manager = SyncManager(
        SyncConfig(
            servers=("time.example.com",),
            quorum=1,
            cycle=60.0,
            stop_timeout=2.0,
        ),
        client=BlockingClient(),
    )
    client = TestClient(create_app(manager, lambda: None))

    assert client.post("/start").status_code == 200
    assert entered.wait(1.0)
    assert client.get("/ping").json() == {"status": "pong"}

    release.set()
    assert client.post("/end").status_code == 200

