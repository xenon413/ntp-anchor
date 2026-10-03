"""Public core orchestration without import-time platform side effects."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
import threading

from .backend_factory import create_time_sync_backend
from .config import DEFAULT_NTP_SERVERS, SyncConfig
from .exceptions import WorkerStateError
from .manager import SyncManager, TimeSyncBackend
from .models import NTPQueryResult, SyncResult
from .ntp_client import NTPClient, NTPQueryClient


_default_manager: SyncManager | None = None
_default_manager_lock = threading.RLock()


def create_sync_manager(
    config: SyncConfig | None = None,
    *,
    client: NTPQueryClient | None = None,
    backend: TimeSyncBackend | None = None,
    os_name: str | None = None,
) -> SyncManager:
    """Create an explicitly owned manager with lazy backend initialization."""

    selected_config = config if config is not None else SyncConfig()
    backend_factory = None
    if backend is None:
        backend_factory = lambda: create_time_sync_backend(
            os_name,
            command_timeout=selected_config.command_timeout,
        )
    return SyncManager(
        selected_config,
        client=client,
        backend=backend,
        backend_factory=backend_factory,
    )


def get_default_manager() -> SyncManager:
    """Return the process-local compatibility manager, creating it lazily."""

    global _default_manager
    with _default_manager_lock:
        if _default_manager is None:
            _default_manager = create_sync_manager()
        return _default_manager


def configure_default_manager(
    config: SyncConfig,
    *,
    client: NTPQueryClient | None = None,
    backend: TimeSyncBackend | None = None,
    os_name: str | None = None,
) -> SyncManager:
    """Replace the default manager while it is stopped.

    Applications that need independently configured controllers should
    construct SyncManager directly. Clock operations remain serialized
    process-wide even across manager instances.
    """

    global _default_manager
    with _default_manager_lock:
        if _default_manager is not None and _default_manager.snapshot().is_running:
            raise WorkerStateError(
                "the default sync manager cannot be replaced while it is running"
            )
        _default_manager = create_sync_manager(
            config,
            client=client,
            backend=backend,
            os_name=os_name,
        )
        return _default_manager


def measure_ntp(
    config: SyncConfig | None = None,
    *,
    client: NTPQueryClient | None = None,
) -> NTPQueryResult:

    # use default if no extra config
    if config is None and client is None:
        return get_default_manager().measure_once()

    # use configed manager if extra value is given
    return SyncManager(
        config=config or SyncConfig(),
        client=client or NTPClient(),
    ).measure_once()


def get_ntp_time_offset(
    ntp_server: Sequence[str] | None = None,
    *,
    ntp_port: int = 123,
    timeout: float = 2.0,
    quorum: int = 2,
) -> float:
    """Return the validated consensus offset for compatibility callers.

    A one-server deployment must explicitly pass quorum=1. New code that
    needs source and quality metadata should use measure_ntp().
    """

    servers = DEFAULT_NTP_SERVERS if ntp_server is None else ntp_server
    config = SyncConfig(
        servers=servers,
        ntp_port=ntp_port,
        query_timeout=timeout,
        quorum=quorum,
    )
    return measure_ntp(config).offset


def start_time_service() -> None:
    """Prepare the lazy platform backend for the configured authorities."""

    get_default_manager().start_time_service()


def sync_time() -> None:
    """Run the legacy direct sync operation under the process-wide lock.

    Prefer check_and_sync(), which enforces threshold, maximum correction, and
    post-sync verification.
    """

    get_default_manager().sync_time()


def check_and_sync(
    ntp_server: Sequence[str] | None = None,
    thresh: float | None = None,
    auto_start: bool | None = None,
    *,
    quorum: int | None = None,
    max_correction: float | None = None,
    manager: SyncManager | None = None,
) -> SyncResult:
    """Measure, conditionally synchronize, and verify clock convergence."""

    # case not using a default manager
    if manager is not None:
        if any(
            value is not None
            for value in (
                ntp_server,
                thresh,
                auto_start,
                quorum,
                max_correction,
            )
        ):
            raise ValueError(
                "manager cannot be combined with per-call configuration overrides"
            )
        return manager.check_once()

    # case all value arguments are None use default manager
    if all(
        value is None
        for value in (
            ntp_server,
            thresh,
            auto_start,
            quorum,
            max_correction,
        )
    ):
        return get_default_manager().check_once()

    # overrides the given argument from default
    base = get_default_manager().config
    overrides: dict[str, object] = {}
    if ntp_server is not None:
        overrides["servers"] = ntp_server
    if thresh is not None:
        overrides["thresh"] = thresh
    if auto_start is not None:
        overrides["auto_start_service"] = auto_start
    if quorum is not None:
        overrides["quorum"] = quorum
    if max_correction is not None:
        overrides["max_correction"] = max_correction

    operation_config = replace(base, **overrides)
    return create_sync_manager(operation_config).check_once()


def check_and_sync_thread(
    ntp_server: Sequence[str] | None = None,
    thresh: float | None = None,
    cycle: float | None = None,
    auto_start: bool | None = None,
    *,
    quorum: int | None = None,
    max_correction: float | None = None,
) -> tuple[threading.Thread, threading.Event]:
    """Start the managed worker and expose temporary legacy handles.

    New code should own a SyncManager and call start()/stop() rather than
    manipulating a raw event.
    """

    # Configuration replacement and worker start are one process-local
    # transaction. Without this lock, two compatibility callers could replace
    # the singleton independently and leave two scheduler threads alive.
    with _default_manager_lock:
        manager = get_default_manager()
        base = manager.config
        overrides: dict[str, object] = {}
        if ntp_server is not None:
            overrides["servers"] = ntp_server
        if thresh is not None:
            overrides["thresh"] = thresh
        if cycle is not None:
            overrides["cycle"] = cycle
        if auto_start is not None:
            overrides["auto_start_service"] = auto_start
        if quorum is not None:
            overrides["quorum"] = quorum
        if max_correction is not None:
            overrides["max_correction"] = max_correction

        requested_config = replace(base, **overrides)
        if requested_config != base:
            manager = configure_default_manager(requested_config)

        manager.start()
        thread = manager.thread
        if thread is None:
            raise WorkerStateError("the managed NTP worker did not start")
        return thread, manager.stop_event


__all__ = [
    "check_and_sync",
    "check_and_sync_thread",
    "configure_default_manager",
    "create_sync_manager",
    "get_default_manager",
    "get_ntp_time_offset",
    "measure_ntp",
    "start_time_service",
    "sync_time",
]
