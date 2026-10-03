"""Thread-safe policy engine and lifecycle owner for periodic NTP checks."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
import logging
import math
import random
import threading
import time
from typing import Protocol

from .config import SyncConfig
from .exceptions import (
    ConfigurationError,
    NTPAnchorError,
    NTPQueryError,
    SyncVerificationError,
    TimeServiceUnavailableError,
    UnsafeCorrectionError,
    UnexpectedError,
    WorkerStateError,
    WorkerStopTimeoutError,
)
from .models import (
    CheckStatus,
    LifecycleState,
    NTPQueryResult,
    SyncResult,
    WorkerDiagnostics,
    WorkerSnapshot,
)
from .ntp_client import NTPClient, NTPQueryClient


logger = logging.getLogger(__name__)

# The host clock is process-global, so manager instances must not own separate
# mutation locks.  Phase 2 intentionally permits only one application process;
# an OS-level lock is required before multi-process operation can be enabled.
_PROCESS_SYNC_LOCK = threading.Lock()


class TimeSyncBackend(Protocol):
    """Privileged platform operation seam used by ``SyncManager``."""

    def start_time_service(self) -> None: ...

    def prepare_time_sync(self, servers: Sequence[str]) -> None: ...

    def sync_time(self, servers: Sequence[str]) -> None: ...


BackendFactory = Callable[[], TimeSyncBackend]
ThreadFactory = Callable[..., threading.Thread]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _CheckCancelled(Exception):
    """Internal control flow used when the managed worker is stopping."""


class _NotifyingEvent(threading.Event):
    """An Event whose ``set`` also wakes the manager condition."""

    def __init__(self, notify: Callable[[], None]) -> None:
        super().__init__()
        self._notify = notify

    def set(self) -> None:
        super().set()
        self._notify()


class SyncManager:
    """Own one synchronization worker and all observable runtime state.

    Measurement, policy evaluation, platform mutation, and verification are
    covered by one single-flight lock.  This prevents a local clock mutation
    from corrupting timestamps used by a concurrent NTP measurement.
    """

    def __init__(
        self,
        config: SyncConfig | None = None,
        *,
        client: NTPQueryClient | None = None,
        backend: TimeSyncBackend | None = None,
        backend_factory: BackendFactory | None = None,
        utc_now: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        random_source: Callable[[], float] = random.random,
        sleep: Callable[[float], None] = time.sleep,
        thread_factory: ThreadFactory = threading.Thread,
    ) -> None:
        if backend is not None and backend_factory is not None:
            raise ConfigurationError("provide backend or backend_factory, not both")

        self._config = config if config is not None else SyncConfig()
        self._client = client if client is not None else NTPClient(utc_now=utc_now)
        self._backend = backend
        self._backend_factory = backend_factory
        self._backend_init_lock = threading.Lock()

        self._utc_now = utc_now
        self._monotonic = monotonic
        self._random = random_source
        self._sleep = sleep
        self._thread_factory = thread_factory

        self._state_lock = threading.RLock()
        self._condition = threading.Condition(self._state_lock)
        self._lifecycle_operation_lock = threading.Lock()
        self._single_flight_lock = _PROCESS_SYNC_LOCK

        self._thread: threading.Thread | None = None
        self._stop_event: _NotifyingEvent = self._new_stop_event()
        self._lifecycle = LifecycleState.STOPPED
        self._cycle_generation = 0

        self._last_checked_at: datetime | None = None
        self._last_synced_at: datetime | None = None
        self._last_check_status = CheckStatus.NEVER
        self._last_measured_offset: float | None = None
        self._last_error: str | None = None
        self._consecutive_failures = 0

    @property
    def config(self) -> SyncConfig:
        with self._state_lock:
            return self._config

    @property
    def thread(self) -> threading.Thread | None:
        with self._state_lock:
            return self._thread

    @property
    def stop_event(self) -> threading.Event:
        """Current generation's stop signal, exposed for legacy adapters."""

        with self._state_lock:
            return self._stop_event

    def snapshot(self) -> WorkerSnapshot:
        with self._state_lock:
            thread = self._thread
            is_running = thread is not None and thread.is_alive()
            return WorkerSnapshot(
                is_running=is_running,
                cycle=self._config.cycle,
                thresh=self._config.thresh,
                last_checked_at=self._last_checked_at,
                last_synced_at=self._last_synced_at,
                last_check_status=self._last_check_status,
                last_measured_offset=self._last_measured_offset,
            )

    def diagnostics(self) -> WorkerDiagnostics:
        with self._state_lock:
            return WorkerDiagnostics(
                lifecycle=self._lifecycle,
                consecutive_failures=self._consecutive_failures,
                last_error=self._last_error,
            )

    def start(self) -> WorkerSnapshot:
        """Start one worker, or return the current state if already running."""

        with self._lifecycle_operation_lock:
            with self._condition:
                if self._thread is not None and self._thread.is_alive():
                    return self.snapshot()

                self._lifecycle = LifecycleState.STARTING
                self._stop_event = self._new_stop_event()
                try:
                    worker = self._thread_factory(
                        target=self._worker_main,
                        args=(self._stop_event,),
                        name="ntp-anchor-sync",
                        daemon=True,
                    )
                    self._thread = worker
                    worker.start()
                except Exception as error:
                    self._thread = None
                    self._lifecycle = LifecycleState.FAILED
                    self._last_check_status = CheckStatus.WORKER_FAILED
                    self._last_error = self._safe_error(error)
                    self._consecutive_failures += 1
                    raise WorkerStateError("failed to start the NTP sync worker") from error

                self._lifecycle = LifecycleState.RUNNING
                return self.snapshot()

    def stop(self, timeout: float | None = None) -> WorkerSnapshot:
        """Signal and join the worker, raising if it remains alive."""

        join_timeout = self._validate_stop_timeout(timeout)
        with self._lifecycle_operation_lock:
            with self._condition:
                worker = self._thread
                if worker is None or not worker.is_alive():
                    self._thread = None
                    self._lifecycle = LifecycleState.STOPPED
                    return self.snapshot()

                self._lifecycle = LifecycleState.STOPPING
                self._stop_event.set()
                if worker is threading.current_thread():
                    raise WorkerStateError("the NTP sync worker cannot join itself")

            # Never hold the state lock while waiting for the worker.  Its
            # finally block needs the same lock to publish the stopped state.
            worker.join(join_timeout)

            with self._condition:
                if worker.is_alive():
                    raise WorkerStopTimeoutError(join_timeout)
                if self._thread is worker:
                    self._thread = None
                self._lifecycle = LifecycleState.STOPPED
                return self.snapshot()

    def set_cycle(self, seconds: float) -> WorkerSnapshot:
        """Atomically update the period and wake a waiting worker."""

        with self._condition:
            updated = self._config.with_cycle(seconds)
            self._config = updated
            self._cycle_generation += 1
            self._condition.notify_all()
            return self.snapshot()

    def set_thresh(self, seconds: float) -> WorkerSnapshot:
        """Atomically update the threshold used by the next policy decision."""

        with self._state_lock:
            updated = self._config.with_thresh(seconds)
            self._config = updated
            return self.snapshot()

    def check_once(self) -> SyncResult:
        """Run one serialized check and return its structured success result."""

        return self._check_once(stop_event=None)

    def measure_once(self) -> NTPQueryResult:
        """Return one measurement without allowing a concurrent clock step."""

        operation_config = self.config
        if not self._single_flight_lock.acquire(
            timeout=operation_config.single_flight_timeout
        ):
            raise WorkerStateError(
                "another NTP or time-service operation is already active"
            )
        try:
            return self._client.query(operation_config)
        finally:
            self._single_flight_lock.release()

    def start_time_service(self) -> None:
        """Prepare the configured platform provider under the global lock."""

        operation_config = self.config
        if not self._single_flight_lock.acquire(
            timeout=operation_config.single_flight_timeout
        ):
            raise WorkerStateError(
                "another NTP or time-service operation is already active"
            )
        try:
            self._get_backend().prepare_time_sync(operation_config.servers)
        finally:
            self._single_flight_lock.release()

    def sync_time(self) -> None:
        """Run the legacy direct sync operation under the process-wide lock.

        New code should prefer :meth:`check_once`, which applies the threshold,
        maximum-correction guard, and post-sync verification.  This method is
        retained so existing callers can migrate without concurrent mutations.
        """

        operation_config = self.config
        if not self._single_flight_lock.acquire(
            timeout=operation_config.single_flight_timeout
        ):
            raise WorkerStateError(
                "another NTP or time-service operation is already active"
            )
        try:
            self._get_backend().sync_time(operation_config.servers)
        finally:
            self._single_flight_lock.release()

    def _check_once(self, stop_event: threading.Event | None) -> SyncResult:
        lock_timeout = self.config.single_flight_timeout
        if not self._single_flight_lock.acquire(timeout=lock_timeout):
            error = WorkerStateError(
                f"another NTP check is still active after {lock_timeout:.3f} seconds"
            )
            self._finish_failure(CheckStatus.OPERATION_BUSY, error, offset=None)
            raise error

        measurement: NTPQueryResult | None = None
        try:
            operation_config = self._begin_check()
            try:
                measurement = self._client.query(operation_config)
                if not isinstance(measurement, NTPQueryResult):
                    raise UnexpectedError(
                        "the NTP query client returned an invalid result"
                    )
            except Exception as error:
                self._finish_failure(CheckStatus.QUERY_FAILED, error, offset=None)
                raise

            self._raise_if_stopping(stop_event, measurement.offset)
            if abs(measurement.offset) <= operation_config.thresh:
                checked_at = self._finish_success(
                    CheckStatus.WITHIN_THRESHOLD,
                    offset=measurement.offset,
                    synced=False,
                )
                return SyncResult(
                    status=CheckStatus.WITHIN_THRESHOLD,
                    measurement=measurement,
                    verification=None,
                    checked_at=checked_at,
                    synced_at=None,
                    correction_attempted=False,
                )

            if abs(measurement.offset) > operation_config.max_correction:
                error = UnsafeCorrectionError(
                    measurement.offset,
                    operation_config.max_correction,
                )
                self._finish_failure(
                    CheckStatus.UNSAFE_CORRECTION,
                    error,
                    offset=measurement.offset,
                )
                raise error

            correction_servers = tuple(
                item.server
                for item in measurement.measurements
                if abs(item.offset) <= operation_config.max_correction
            )
            if len(correction_servers) < operation_config.quorum:
                unsafe_offset = max(
                    measurement.measurements,
                    key=lambda item: abs(item.offset),
                ).offset
                error = UnsafeCorrectionError(
                    unsafe_offset,
                    operation_config.max_correction,
                )
                self._finish_failure(
                    CheckStatus.UNSAFE_CORRECTION,
                    error,
                    offset=measurement.offset,
                )
                raise error

            if operation_config.ntp_port != 123:
                error = TimeServiceUnavailableError(
                    "clock correction through platform tools is unavailable "
                    "for a non-standard NTP port"
                )
                self._finish_failure(
                    CheckStatus.SYNC_FAILED,
                    error,
                    offset=measurement.offset,
                )
                raise error

            try:
                backend = self._get_backend()
                self._raise_if_stopping(stop_event, measurement.offset)
                if operation_config.auto_start_service:
                    backend.prepare_time_sync(correction_servers)
                self._raise_if_stopping(stop_event, measurement.offset)
                backend.sync_time(correction_servers)
                self._raise_if_stopping(stop_event, measurement.offset)
            except _CheckCancelled:
                raise
            except Exception as error:
                self._finish_failure(CheckStatus.SYNC_FAILED, error, offset=measurement.offset)
                raise

            return self._verify_sync(measurement, operation_config, stop_event)
        except _CheckCancelled:
            raise
        finally:
            self._single_flight_lock.release()

    def _verify_sync(
        self,
        measurement: NTPQueryResult,
        operation_config: SyncConfig,
        stop_event: threading.Event | None,
    ) -> SyncResult:
        latest: NTPQueryResult | None = None
        last_error: Exception | None = None
        for attempt in range(operation_config.verification_attempts):
            self._raise_if_stopping(
                stop_event,
                latest.offset if latest is not None else measurement.offset,
            )
            if attempt:
                self._wait_between_verification_attempts(
                    operation_config.verification_interval,
                    stop_event,
                    latest.offset if latest is not None else measurement.offset,
                )

            try:
                latest = self._client.query(operation_config)
                if not isinstance(latest, NTPQueryResult):
                    raise UnexpectedError(
                        "the NTP query client returned an invalid verification result"
                    )
                last_error = None
            except NTPQueryError as error:
                last_error = error
                continue
            except Exception as error:
                self._finish_failure(
                    CheckStatus.VERIFICATION_FAILED,
                    error,
                    offset=(
                        latest.offset
                        if latest is not None
                        else measurement.offset
                    ),
                )
                raise

            self._raise_if_stopping(stop_event, latest.offset)
            if abs(latest.offset) <= operation_config.thresh:
                checked_at = self._finish_success(
                    CheckStatus.SYNC_SUCCEEDED,
                    offset=latest.offset,
                    synced=True,
                )
                return SyncResult(
                    status=CheckStatus.SYNC_SUCCEEDED,
                    measurement=measurement,
                    verification=latest,
                    checked_at=checked_at,
                    synced_at=checked_at,
                    correction_attempted=True,
                )

        latest_offset = latest.offset if latest is not None else measurement.offset
        error = SyncVerificationError(latest_offset, operation_config.thresh)
        if last_error is not None:
            error.__cause__ = last_error
        self._finish_failure(
            CheckStatus.VERIFICATION_FAILED,
            error,
            offset=latest_offset,
        )
        raise error

    def _worker_main(self, stop_event: threading.Event) -> None:
        fatal_error: BaseException | None = None
        try:
            while not stop_event.is_set():
                succeeded = False
                try:
                    self._check_once(stop_event)
                    succeeded = True
                except _CheckCancelled:
                    break
                except NTPAnchorError as error:
                    # Expected operational failures may contain bounded but
                    # sensitive command output. Keep logs sanitized too.
                    logger.warning(
                        "periodic NTP check failed: %s",
                        self._safe_error(error),
                    )
                except Exception:
                    logger.exception("periodic NTP check failed unexpectedly")

                if stop_event.is_set():
                    break

                completed_at = self._monotonic()
                if succeeded:
                    self._wait_for_cycle(completed_at, stop_event)
                else:
                    self._wait_for_backoff(stop_event)
        except BaseException as error:
            fatal_error = error
            with self._condition:
                self._lifecycle = LifecycleState.FAILED
                self._last_check_status = CheckStatus.WORKER_FAILED
                self._last_error = self._safe_error(error)
                self._consecutive_failures += 1
            logger.exception("NTP sync worker terminated unexpectedly")
        finally:
            with self._condition:
                if self._thread is threading.current_thread():
                    if fatal_error is None:
                        self._lifecycle = LifecycleState.STOPPED
                    self._thread = None
                    self._condition.notify_all()

    def _wait_for_cycle(self, completed_at: float, stop_event: threading.Event) -> None:
        with self._condition:
            generation = self._cycle_generation
            deadline = completed_at + self._config.cycle
            while not stop_event.is_set():
                remaining = deadline - self._monotonic()
                if remaining <= 0.0:
                    return
                self._condition.wait(timeout=remaining)
                if self._cycle_generation != generation:
                    generation = self._cycle_generation
                    deadline = completed_at + self._config.cycle

    def _wait_for_backoff(self, stop_event: threading.Event) -> None:
        with self._condition:
            failures = max(1, self._consecutive_failures)
            config = self._config
            exponent = min(failures - 1, 30)
            base = min(config.retry_max_delay, config.retry_base_delay * (2**exponent))
            random_value = min(1.0, max(0.0, float(self._random())))
            multiplier = 1.0 - config.retry_jitter + (2.0 * config.retry_jitter * random_value)
            delay = min(config.retry_max_delay, base * multiplier)
            self._condition.wait_for(stop_event.is_set, timeout=delay)

    def _wait_between_verification_attempts(
        self,
        delay: float,
        stop_event: threading.Event | None,
        offset: float,
    ) -> None:
        if delay <= 0.0:
            return
        if stop_event is None:
            self._sleep(delay)
            return
        if stop_event.wait(delay):
            self._cancel_check(offset)

    def _get_backend(self) -> TimeSyncBackend:
        if self._backend is not None:
            return self._backend
        with self._backend_init_lock:
            if self._backend is None:
                if self._backend_factory is None:
                    raise TimeServiceUnavailableError(
                        "no clock synchronization backend is configured"
                    )
                try:
                    self._backend = self._backend_factory()
                except NTPAnchorError:
                    raise
                except Exception as error:
                    raise TimeServiceUnavailableError(
                        "failed to initialize the clock synchronization backend"
                    ) from error
        return self._backend

    def _begin_check(self) -> SyncConfig:
        """Atomically mark a check active and capture its immutable policy."""

        with self._state_lock:
            operation_config = self._config
            self._last_check_status = CheckStatus.CHECKING
            self._last_measured_offset = None
            self._last_error = None
            return operation_config

    def _finish_success(
        self,
        status: CheckStatus,
        *,
        offset: float,
        synced: bool,
    ) -> datetime:
        checked_at = self._aware_utc_now()
        with self._state_lock:
            self._last_checked_at = checked_at
            if synced:
                self._last_synced_at = checked_at
            self._last_check_status = status
            self._last_measured_offset = offset
            self._last_error = None
            self._consecutive_failures = 0
        return checked_at

    def _finish_failure(
        self,
        status: CheckStatus,
        error: BaseException,
        *,
        offset: float | None,
    ) -> None:
        checked_at = self._aware_utc_now()
        with self._state_lock:
            self._last_checked_at = checked_at
            self._last_check_status = status
            self._last_measured_offset = offset
            self._last_error = self._safe_error(error)
            self._consecutive_failures += 1

    def _raise_if_stopping(
        self,
        stop_event: threading.Event | None,
        offset: float,
    ) -> None:
        if stop_event is not None and stop_event.is_set():
            self._cancel_check(offset)

    def _cancel_check(self, offset: float) -> None:
        checked_at = self._aware_utc_now()
        with self._state_lock:
            self._last_checked_at = checked_at
            self._last_check_status = CheckStatus.CANCELLED
            self._last_measured_offset = offset
            self._last_error = None
        raise _CheckCancelled()

    def _validate_stop_timeout(self, timeout: float | None) -> float:
        value = self.config.stop_timeout if timeout is None else timeout
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigurationError("stop timeout must be a finite positive number")
        result = float(value)
        if not math.isfinite(result) or result <= 0.0:
            raise ConfigurationError("stop timeout must be a finite positive number")
        return result

    def _aware_utc_now(self) -> datetime:
        value = self._utc_now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise UnexpectedError("utc_now must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _new_stop_event(self) -> _NotifyingEvent:
        return _NotifyingEvent(self._notify_waiters)

    def _notify_waiters(self) -> None:
        with self._condition:
            self._condition.notify_all()

    @staticmethod
    def _safe_error(error: BaseException) -> str:
        return f"{type(error).__name__}: operation failed"


__all__ = ["BackendFactory", "SyncManager", "TimeSyncBackend"]
