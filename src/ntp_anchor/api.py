"""FastAPI management surface and single-process Uvicorn launcher.

Importing this module constructs no application, socket, server, or worker.
Call :func:`create_app` with an explicitly owned ``SyncManager`` for ASGI use,
or :func:`run_server` to use the retained-socket Uvicorn integration.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import ipaddress
import logging
import math
import socket
import threading
from typing import Annotated, Any, Literal, Protocol, cast
from uuid import uuid4

from . import __version__
from ._api_config import (
    DEFAULT_API_HOST,
    DEFAULT_API_LOG_FILE,
    DEFAULT_API_LOG_LEVEL,
    DEFAULT_API_PORT,
    MAX_CYCLE_SECONDS,
    validate_allow_unbounded_correction as _validate_allow_unbounded_correction,
    validate_api_host as _validate_host,
    validate_api_port as _validate_port,
    validate_log_file as _validate_log_file,
    validate_log_level as _validate_log_level,
    validate_server_mode as _validate_server_mode,
)
from ._cli import main as _cli_main
from ._logging import configure_service_logging
from .core.backends import (
    detect_time_sync_privilege as _detect_time_sync_privilege,
)
from .core.config import SyncConfig
from .core.core_logic import timestamp as _timestamp
from .core.exceptions import (
    ConfigurationError,
    NTPAnchorError,
    PortInUseError,
    TimeServiceCommandError,
    TimeServiceTimeoutError,
    TimeServiceUnavailableError,
    UnsafeCorrectionError,
    UnexpectedError,
    UnsupportedOSError,
    WorkerStateError,
    WorkerStopTimeoutError,
)
from .core.manager import SyncManager
from .core.models import (
    CheckStatus,
    NTPQueryResult,
    SyncResult,
    WorkerSnapshot,
)
from .core.privileges import TimeSyncPrivilegeCheck, TimeSyncPrivilegeStatus


try:
    from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
    from fastapi.concurrency import run_in_threadpool
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator
    import uvicorn
except ImportError:
    if __name__ == "__main__":
        # Delegate through the dependency-light launcher so module invocation
        # reports the same stable exit code and installation hint as the
        # console script when the optional web stack is unavailable.
        raise SystemExit(_cli_main()) from None
    raise


logger = logging.getLogger(__name__)

_SOCKET_BACKLOG = 2_048
_CORRECTION_PRIVILEGE_DETAIL = (
    "Clock correction is unavailable because the service lacks the required "
    "host privileges."
)


class _ApiModel(BaseModel):
    """Strict base for stable management API contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def _strict_finite_number(value: object) -> float:
    """Accept JSON numbers while rejecting coercion, Booleans, and non-finite values."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("value must be a JSON number")
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError("value is too large") from error
    if not math.isfinite(result):
        raise ValueError("value must be finite")
    return result


class CycleRequest(_ApiModel):
    """Validated request for the periodic check interval."""

    cycle: Annotated[
        float,
        BeforeValidator(_strict_finite_number),
        Field(
            gt=0.0,
            le=MAX_CYCLE_SECONDS,
            description="Periodic check interval in seconds (maximum 24 hours).",
        ),
    ]


class ThreshRequest(_ApiModel):
    """Validated request for the clock-drift threshold."""

    thresh: Annotated[
        float,
        BeforeValidator(_strict_finite_number),
        Field(
            ge=0.0,
            description=(
                "Clock-drift threshold in seconds; it may not exceed the "
                "manager's current maximum correction when that guard is enabled."
            ),
        ),
    ]


class MaxCorrectionRequest(_ApiModel):
    """Validated runtime correction-limit update."""

    max_correction: Annotated[
        float,
        BeforeValidator(_strict_finite_number),
        Field(
            description=(
                "Finite maximum automatic correction in seconds. Exact -1 "
                "disables this guard when the server explicitly allows it."
            ),
        ),
    ]

    @field_validator("max_correction")
    @classmethod
    def _validate_max_correction(cls, value: float) -> float:
        if value == -1.0 or value >= 0.0:
            return value
        raise ValueError("max_correction must be -1 or non-negative")


class PingResponse(_ApiModel):
    status: Literal["pong"]


class TimestampResponse(_ApiModel):
    timestamp: Annotated[
        float,
        Field(
            description=(
                "Current system time in Unix epoch seconds, including fractional "
                "seconds. This clock may move backward when adjusted."
            ),
        ),
    ]


class RunningResponse(_ApiModel):
    is_running: bool


class CloseResponse(_ApiModel):
    is_running: bool
    is_closing: bool


class CycleResponse(_ApiModel):
    cycle: Annotated[float, Field(gt=0.0, le=MAX_CYCLE_SECONDS)]
    is_running: bool


class ThreshResponse(_ApiModel):
    thresh: Annotated[float, Field(ge=0.0)]
    is_running: bool


class MaxCorrectionResponse(_ApiModel):
    max_correction: Annotated[float | None, Field(ge=0.0)]
    max_correction_enabled: bool
    is_running: bool


class OffsetResponse(_ApiModel):
    offset: float
    queried_at: datetime
    source_count: Annotated[int, Field(ge=1)]


class SyncResponse(_ApiModel):
    status: CheckStatus
    initial_offset: float
    final_offset: float
    correction_attempted: bool
    checked_at: datetime
    synced_at: datetime | None


class StatusResponse(_ApiModel):
    """The exact seven-field public worker snapshot."""

    is_running: bool
    cycle: Annotated[float, Field(gt=0.0)]
    thresh: Annotated[float, Field(ge=0.0)]
    last_checked_at: datetime | None
    last_synced_at: datetime | None
    last_check_status: CheckStatus
    last_measured_offset: float | None

    @field_validator("last_checked_at", "last_synced_at")
    @classmethod
    def _normalize_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("status timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @classmethod
    def from_snapshot(cls, snapshot: WorkerSnapshot) -> StatusResponse:
        return cls(
            is_running=snapshot.is_running,
            cycle=snapshot.cycle,
            thresh=snapshot.thresh,
            last_checked_at=snapshot.last_checked_at,
            last_synced_at=snapshot.last_synced_at,
            last_check_status=snapshot.last_check_status,
            last_measured_offset=snapshot.last_measured_offset,
        )


class HelpParameter(_ApiModel):
    name: str
    location: Literal["body"]
    required: bool
    type: Literal["number"]
    constraints: dict[str, bool | float]


class RouteHelp(_ApiModel):
    path: str
    method: Literal["GET", "POST"]
    parameters: tuple[HelpParameter, ...]
    description: str


class HelpResponse(_ApiModel):
    routes: tuple[RouteHelp, ...]


@dataclass(frozen=True, slots=True)
class _RouteDefinition:
    name: str
    help: RouteHelp
    endpoint: Callable[..., BaseModel]
    response_model: type[BaseModel]


class _ManagerProtocol(Protocol):
    @property
    def config(self) -> SyncConfig: ...

    def snapshot(self) -> WorkerSnapshot: ...

    def start(self) -> WorkerSnapshot: ...

    def stop(self, timeout: float | None = None) -> WorkerSnapshot: ...

    def check_once(self) -> SyncResult: ...

    def measure_once(self) -> NTPQueryResult: ...

    def set_cycle(self, seconds: float) -> WorkerSnapshot: ...

    def set_thresh(self, seconds: float) -> WorkerSnapshot: ...

    def set_max_correction(
        self,
        seconds: float | None,
    ) -> WorkerSnapshot: ...


CloseAuthorizer = Callable[[Request], bool]


class _ApiLifecycleConflictError(NTPAnchorError):
    """A genuine conflict between API lifecycle operations."""


class _ApiLifecycle:
    """Serialize API lifecycle mutations and protect the closing transition."""

    def __init__(self, manager: _ManagerProtocol) -> None:
        self._manager = manager
        self._lock = threading.RLock()
        self._closing = False

    @property
    def manager(self) -> _ManagerProtocol:
        return self._manager

    def snapshot(self) -> WorkerSnapshot:
        return self._manager.snapshot()

    def start(self) -> WorkerSnapshot:
        with self._lock:
            if self._closing:
                raise _ApiLifecycleConflictError(
                    "the management server is already closing"
                )
            return self._manager.start()

    def stop(self) -> WorkerSnapshot:
        with self._lock:
            return self._manager.stop()

    def sync_once(self) -> SyncResult:
        with self._lock:
            if self._closing:
                raise _ApiLifecycleConflictError(
                    "the management server is already closing"
                )
            return self._manager.check_once()

    def measure_once(self) -> NTPQueryResult:
        with self._lock:
            if self._closing:
                raise _ApiLifecycleConflictError(
                    "the management server is already closing"
                )
            return self._manager.measure_once()

    def close(self) -> WorkerSnapshot:
        with self._lock:
            if self._closing:
                return self._manager.snapshot()

            # Publish closing before the potentially blocking stop. A racing
            # /start waits on this lock and then observes the closing state.
            self._closing = True
            try:
                snapshot = self._manager.stop()
                if snapshot.is_running:
                    raise WorkerStopTimeoutError(
                        "Background sync worker is still running after stop."
                    )
                return snapshot
            except Exception:
                # A failed stop must keep the server available for diagnosis
                # and another bounded stop attempt.
                self._closing = False
                raise

    def set_cycle(self, seconds: float) -> WorkerSnapshot:
        with self._lock:
            if self._closing:
                raise _ApiLifecycleConflictError(
                    "the management server is already closing"
                )
            return self._manager.set_cycle(seconds)

    def set_thresh(self, seconds: float) -> WorkerSnapshot:
        with self._lock:
            if self._closing:
                raise _ApiLifecycleConflictError(
                    "the management server is already closing"
                )
            # Preflight the dynamic threshold/correction invariant before
            # invoking the manager. The manager repeats this validation as a
            # final guard, but API-invalid requests must not reach a mutator.
            self._manager.config.with_thresh(seconds)
            return self._manager.set_thresh(seconds)

    def set_max_correction(
        self,
        seconds: float | None,
    ) -> WorkerSnapshot:
        with self._lock:
            if self._closing:
                raise _ApiLifecycleConflictError(
                    "the management server is already closing"
                )
            self._manager.config.with_max_correction(seconds)
            return self._manager.set_max_correction(seconds)

    def cleanup(self) -> WorkerSnapshot:
        with self._lock:
            self._closing = True
            return self._manager.stop()


class _OnceCallback:
    """Invoke a shutdown callback at most once across concurrent closes."""

    def __init__(self, callback: Callable[[], None]) -> None:
        self._callback = callback
        self._lock = threading.Lock()
        self._called = False

    @property
    def called(self) -> bool:
        with self._lock:
            return self._called

    def invoke(self) -> None:
        with self._lock:
            if self._called:
                return
            self._called = True

        try:
            self._callback()
        except Exception as error:
            incident_id = uuid4().hex
            logger.exception(
                "server shutdown callback failed (incident_id=%s, error_type=%s)",
                incident_id,
                type(error).__name__,
            )
        else:
            logger.info("management server shutdown signal sent")


class _UvicornServerProtocol(Protocol):
    should_exit: bool


class UvicornShutdownAdapter:
    """Narrow adapter that exposes only Uvicorn's graceful-exit signal."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._server: _UvicornServerProtocol | None = None

    def attach(self, server: _UvicornServerProtocol) -> None:
        with self._lock:
            if self._server is not None and self._server is not server:
                raise RuntimeError("a Uvicorn server is already attached")
            self._server = server

    def request_shutdown(self) -> None:
        with self._lock:
            if self._server is None:
                raise RuntimeError("the Uvicorn server is not attached")
            self._server.should_exit = True


def _lifecycle(request: Request) -> _ApiLifecycle:
    return cast(_ApiLifecycle, request.app.state.ntp_anchor_lifecycle)


def _require_correction_privilege(request: Request) -> None:
    check = cast(
        TimeSyncPrivilegeCheck,
        request.app.state.ntp_anchor_time_sync_privilege,
    )
    if check.correction_available:
        return
    logger.warning(
        "privileged management operation rejected path=%s privilege_status=%s "
        "detection_error_type=%s",
        request.url.path,
        check.status.value,
        check.error_type,
    )
    raise HTTPException(status_code=503, detail=_CORRECTION_PRIVILEGE_DETAIL)


def _log_correction_privilege(check: TimeSyncPrivilegeCheck) -> None:
    if check.status is TimeSyncPrivilegeStatus.UNAVAILABLE:
        logger.warning(
            "clock-correction endpoints disabled; process lacks required host "
            "privileges; read-only management endpoints remain available"
        )
    elif check.status is TimeSyncPrivilegeStatus.UNKNOWN:
        logger.warning(
            "clock-correction privilege detection failed error_type=%s; "
            "treating capability as unavailable; read-only management endpoints "
            "remain available",
            check.error_type,
        )


def _ping_route() -> PingResponse:
    return PingResponse(status="pong")


def _timestamp_route(response: Response) -> TimestampResponse:
    response.headers["Cache-Control"] = "no-store"
    return TimestampResponse(timestamp=_timestamp())


def _help_route(request: Request) -> HelpResponse:
    routes = cast(tuple[RouteHelp, ...], request.app.state.ntp_anchor_routes)
    return HelpResponse(routes=routes)


def _start_route(request: Request) -> RunningResponse:
    _require_correction_privilege(request)
    try:
        snapshot = _lifecycle(request).start()
    except WorkerStateError as error:
        # SyncManager uses WorkerStateError both for conflicts and for a failed
        # thread factory. At this boundary, a /start failure is unavailability.
        raise HTTPException(
            status_code=503,
            detail="The background sync worker could not be started.",
        ) from error
    if not snapshot.is_running:
        raise HTTPException(
            status_code=503,
            detail="The background sync worker could not be started.",
        )
    return RunningResponse(is_running=True)


def _end_route(request: Request) -> RunningResponse:
    snapshot = _lifecycle(request).stop()
    if snapshot.is_running:
        raise WorkerStopTimeoutError(
            "Background sync worker is still running after stop."
        )
    return RunningResponse(is_running=False)


def _close_route(
    request: Request,
    background_tasks: BackgroundTasks,
) -> CloseResponse:
    authorizer = cast(
        CloseAuthorizer,
        request.app.state.ntp_anchor_close_authorizer,
    )
    authorized = authorizer(request)
    if not isinstance(authorized, bool):
        raise TypeError("close_authorizer must return a Boolean")
    if not authorized:
        raise HTTPException(status_code=403, detail="Close access is forbidden.")
    logger.info("authorized management API close requested")
    snapshot = _lifecycle(request).close()
    if snapshot.is_running:
        raise WorkerStopTimeoutError(
            "Background sync worker is still running after stop."
        )
    shutdown = cast(_OnceCallback, request.app.state.ntp_anchor_shutdown)
    # Starlette runs background tasks only after sending the response body.
    background_tasks.add_task(shutdown.invoke)
    return CloseResponse(is_running=False, is_closing=True)


def _status_route(request: Request) -> StatusResponse:
    return StatusResponse.from_snapshot(_lifecycle(request).snapshot())


def _sync_route(request: Request) -> SyncResponse:
    _require_correction_privilege(request)
    logger.info("manual NTP synchronization check requested")
    try:
        result = _lifecycle(request).sync_once()
    except WorkerStateError as error:
        logger.warning("manual NTP synchronization rejected; operation busy")
        raise HTTPException(
            status_code=409,
            detail="Another NTP or time-service operation is already active.",
        ) from error
    return SyncResponse(
        status=result.status,
        initial_offset=result.measurement.offset,
        final_offset=result.measured_offset,
        correction_attempted=result.correction_attempted,
        checked_at=result.checked_at,
        synced_at=result.synced_at,
    )


def _offset_route(request: Request) -> OffsetResponse:
    logger.info("manual NTP offset measurement requested")
    try:
        result = _lifecycle(request).measure_once()
    except WorkerStateError as error:
        logger.warning("manual NTP offset measurement rejected; operation busy")
        raise HTTPException(
            status_code=409,
            detail="Another NTP or time-service operation is already active.",
        ) from error
    return OffsetResponse(
        offset=result.offset,
        queried_at=result.queried_at,
        source_count=len(result.measurements),
    )


def _set_cycle_route(payload: CycleRequest, request: Request) -> CycleResponse:
    snapshot = _lifecycle(request).set_cycle(payload.cycle)
    return CycleResponse(cycle=snapshot.cycle, is_running=snapshot.is_running)


def _set_thresh_route(payload: ThreshRequest, request: Request) -> ThreshResponse:
    lifecycle = _lifecycle(request)
    snapshot = lifecycle.set_thresh(payload.thresh)
    return ThreshResponse(thresh=snapshot.thresh, is_running=snapshot.is_running)


def _set_max_correction_route(
    payload: MaxCorrectionRequest,
    request: Request,
) -> MaxCorrectionResponse:
    if payload.max_correction == -1.0:
        if not request.app.state.ntp_anchor_allow_unbounded_correction:
            logger.warning(
                "unbounded correction update rejected; startup opt-in is disabled"
            )
            raise HTTPException(
                status_code=403,
                detail="Unbounded correction is disabled by server policy.",
            )
        value: float | None = None
    else:
        value = payload.max_correction

    snapshot = _lifecycle(request).set_max_correction(value)
    return MaxCorrectionResponse(
        max_correction=value,
        max_correction_enabled=value is not None,
        is_running=snapshot.is_running,
    )


def _route_definitions(
    allow_unbounded_correction: bool,
) -> tuple[_RouteDefinition, ...]:
    no_parameters: tuple[HelpParameter, ...] = ()
    cycle_parameters = (
        HelpParameter(
            name="cycle",
            location="body",
            required=True,
            type="number",
            constraints={
                "finite": True,
                "exclusive_minimum": 0.0,
                "maximum": MAX_CYCLE_SECONDS,
            },
        ),
    )
    thresh_parameters = (
        HelpParameter(
            name="thresh",
            location="body",
            required=True,
            type="number",
            constraints={
                "finite": True,
                "minimum": 0.0,
                "bounded_by_current_max_correction": True,
            },
        ),
    )
    max_correction_parameters = (
        HelpParameter(
            name="max_correction",
            location="body",
            required=True,
            type="number",
            constraints={
                "finite": True,
                "minimum": 0.0,
                "unbounded_sentinel": -1.0,
                "unbounded_allowed": allow_unbounded_correction,
            },
        ),
    )

    entries = (
        (
            "ping",
            "/ping",
            "GET",
            no_parameters,
            "Check whether the management API is responsive.",
            _ping_route,
            PingResponse,
        ),
        (
            "timestamp",
            "/timestamp",
            "GET",
            no_parameters,
            "Return the current system time as Unix epoch seconds.",
            _timestamp_route,
            TimestampResponse,
        ),
        (
            "help",
            "/help",
            "GET",
            no_parameters,
            "List all management routes and their request constraints.",
            _help_route,
            HelpResponse,
        ),
        (
            "start",
            "/start",
            "POST",
            no_parameters,
            "Start periodic NTP checking.",
            _start_route,
            RunningResponse,
        ),
        (
            "end",
            "/end",
            "POST",
            no_parameters,
            "Gracefully stop periodic NTP checking.",
            _end_route,
            RunningResponse,
        ),
        (
            "close",
            "/close",
            "POST",
            no_parameters,
            (
                "Stop periodic checking and gracefully close the management "
                "server from a trusted client."
            ),
            _close_route,
            CloseResponse,
        ),
        (
            "status",
            "/status",
            "GET",
            no_parameters,
            "Return one atomic NTP worker state snapshot.",
            _status_route,
            StatusResponse,
        ),
        (
            "sync",
            "/sync",
            "POST",
            no_parameters,
            "Run one policy-controlled NTP check and correction if needed.",
            _sync_route,
            SyncResponse,
        ),
        (
            "offset",
            "/offset",
            "POST",
            no_parameters,
            "Measure a fresh consensus offset without changing the clock.",
            _offset_route,
            OffsetResponse,
        ),
        (
            "set_cycle",
            "/set-cycle",
            "POST",
            cycle_parameters,
            "Update the periodic NTP check interval.",
            _set_cycle_route,
            CycleResponse,
        ),
        (
            "set_thresh",
            "/set-thresh",
            "POST",
            thresh_parameters,
            "Update the clock-drift threshold for the next check.",
            _set_thresh_route,
            ThreshResponse,
        ),
        (
            "set_max_correction",
            "/set-max-correction",
            "POST",
            max_correction_parameters,
            "Update the optional maximum correction for the next check.",
            _set_max_correction_route,
            MaxCorrectionResponse,
        ),
    )
    return tuple(
        _RouteDefinition(
            name=name,
            help=RouteHelp(
                path=path,
                method=cast(Literal["GET", "POST"], method),
                parameters=parameters,
                description=description,
            ),
            endpoint=endpoint,
            response_model=response_model,
        )
        for (
            name,
            path,
            method,
            parameters,
            description,
            endpoint,
            response_model,
        ) in entries
    )


def _register_exception_handler(
    app: FastAPI,
    exception_type: type[Exception],
    handler: Callable[..., Any],
) -> None:
    """Bridge precise handler types to Starlette's broad handler protocol."""

    app.add_exception_handler(exception_type, cast(Any, handler))


def _validation_error_response(error: RequestValidationError) -> JSONResponse:
    """Return useful 422 details without echoing invalid values such as NaN."""

    details: list[dict[str, object]] = []
    for item in error.errors():
        location = [
            part if isinstance(part, (str, int)) else str(part)
            for part in item.get("loc", ())
        ]
        details.append(
            {
                "type": str(item.get("type", "value_error")),
                "loc": location,
                "msg": str(item.get("msg", "Request validation failed")),
            }
        )
    return JSONResponse(status_code=422, content={"detail": details})


async def _request_validation_handler(
    _request: Request,
    error: RequestValidationError,
) -> JSONResponse:
    return _validation_error_response(error)


async def _configuration_error_handler(
    _request: Request,
    _error: ConfigurationError,
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": "The requested configuration is invalid."},
    )


async def _conflict_error_handler(
    request: Request,
    error: _ApiLifecycleConflictError,
) -> JSONResponse:
    logger.warning(
        "management lifecycle conflict method=%s path=%s error_type=%s",
        request.method,
        request.url.path,
        type(error).__name__,
    )
    return JSONResponse(
        status_code=409,
        content={
            "detail": "The requested lifecycle operation conflicts with shutdown."
        },
    )


async def _timeout_error_handler(
    request: Request,
    error: WorkerStopTimeoutError | TimeServiceTimeoutError,
) -> JSONResponse:
    logger.warning(
        "management operation timed out method=%s path=%s error_type=%s",
        request.method,
        request.url.path,
        type(error).__name__,
    )
    return JSONResponse(
        status_code=504,
        content={"detail": "The operation did not complete before its deadline."},
    )


async def _unsafe_correction_handler(
    request: Request,
    error: UnsafeCorrectionError,
) -> JSONResponse:
    logger.warning(
        "management correction rejected by safety policy method=%s path=%s "
        "error_type=%s",
        request.method,
        request.url.path,
        type(error).__name__,
    )
    return JSONResponse(
        status_code=409,
        content={
            "detail": "Clock correction is prohibited by the current safety policy."
        },
    )


async def _unavailable_error_handler(
    request: Request,
    error: NTPAnchorError,
) -> JSONResponse:
    logger.warning(
        "management operation unavailable method=%s path=%s error_type=%s",
        request.method,
        request.url.path,
        type(error).__name__,
    )
    return JSONResponse(
        status_code=503,
        content={"detail": "The requested operation is currently unavailable."},
    )


async def _unexpected_error_handler(
    _request: Request,
    error: Exception,
) -> JSONResponse:
    incident_id = uuid4().hex
    logger.exception(
        "unexpected management API failure (incident_id=%s, error_type=%s)",
        incident_id,
        type(error).__name__,
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error.",
            "incident_id": incident_id,
        },
    )


def _loopback_close_authorizer(request: Request) -> bool:
    """Allow `/close` only to a loopback peer unless a policy is injected."""

    client = request.client
    if client is None:
        return False
    if client.host == "testclient":
        # Starlette's in-process TestClient uses this non-network sentinel.
        return True
    try:
        address = ipaddress.ip_address(client.host.split("%", 1)[0])
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped is not None and mapped.is_loopback)


def create_app(
    manager: SyncManager,
    shutdown_callback: Callable[[], None],
    *,
    close_authorizer: CloseAuthorizer | None = None,
    allow_unbounded_correction: bool = False,
    time_sync_privilege: TimeSyncPrivilegeCheck | None = None,
) -> FastAPI:
    """Create the management API around one injected manager.

    The returned application does not auto-start the synchronization worker.
    The default close policy accepts loopback peers only; an authenticated
    deployment can inject a narrower trusted policy. :func:`run_server`
    enforces a loopback-only bind for this initial implementation. When no
    privilege result is injected, the application detects and caches one before
    serving requests.
    """

    if manager is None:
        raise TypeError("manager is required")
    if not callable(shutdown_callback):
        raise TypeError("shutdown_callback must be callable")
    if close_authorizer is not None and not callable(close_authorizer):
        raise TypeError("close_authorizer must be callable")
    allow_unbounded = _validate_allow_unbounded_correction(allow_unbounded_correction)
    if manager.config.max_correction is None and not allow_unbounded:
        raise ConfigurationError(
            "an unbounded manager requires explicit server authorization"
        )
    if time_sync_privilege is None:
        privilege_check = _detect_time_sync_privilege()
    elif isinstance(time_sync_privilege, TimeSyncPrivilegeCheck):
        privilege_check = time_sync_privilege
    else:
        raise TypeError("time_sync_privilege must be a TimeSyncPrivilegeCheck")

    lifecycle = _ApiLifecycle(cast(_ManagerProtocol, manager))
    shutdown = _OnceCallback(shutdown_callback)
    authorize_close = close_authorizer or _loopback_close_authorizer

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        logger.debug("management application lifespan started")
        try:
            yield
        finally:
            try:
                await run_in_threadpool(lifecycle.cleanup)
            except Exception as error:
                incident_id = uuid4().hex
                logger.exception(
                    "management lifespan cleanup failed "
                    "(incident_id=%s, error_type=%s)",
                    incident_id,
                    type(error).__name__,
                )
                raise
            else:
                logger.debug("management application lifespan cleanup completed")

    app = FastAPI(
        title="ntp-anchor management API",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.ntp_anchor_manager = manager
    app.state.ntp_anchor_lifecycle = lifecycle
    app.state.ntp_anchor_shutdown = shutdown
    app.state.ntp_anchor_close_authorizer = authorize_close
    app.state.ntp_anchor_allow_unbounded_correction = allow_unbounded
    app.state.ntp_anchor_time_sync_privilege = privilege_check
    _log_correction_privilege(privilege_check)

    route_definitions = _route_definitions(allow_unbounded)
    app.state.ntp_anchor_routes = tuple(item.help for item in route_definitions)
    for definition in route_definitions:
        app.add_api_route(
            definition.help.path,
            definition.endpoint,
            methods=[definition.help.method],
            name=definition.name,
            operation_id=f"ntp_anchor_{definition.name}",
            description=definition.help.description,
            response_model=definition.response_model,
            status_code=200,
        )

    _register_exception_handler(
        app,
        RequestValidationError,
        _request_validation_handler,
    )
    _register_exception_handler(app, ConfigurationError, _configuration_error_handler)
    _register_exception_handler(
        app,
        _ApiLifecycleConflictError,
        _conflict_error_handler,
    )
    _register_exception_handler(app, WorkerStopTimeoutError, _timeout_error_handler)
    _register_exception_handler(app, TimeServiceTimeoutError, _timeout_error_handler)
    _register_exception_handler(
        app,
        UnsafeCorrectionError,
        _unsafe_correction_handler,
    )
    _register_exception_handler(
        app,
        TimeServiceUnavailableError,
        _unavailable_error_handler,
    )
    _register_exception_handler(app, UnsupportedOSError, _unavailable_error_handler)
    _register_exception_handler(
        app,
        TimeServiceCommandError,
        _unavailable_error_handler,
    )
    _register_exception_handler(app, WorkerStateError, _unavailable_error_handler)
    _register_exception_handler(app, UnexpectedError, _unexpected_error_handler)
    _register_exception_handler(app, NTPAnchorError, _unavailable_error_handler)
    _register_exception_handler(app, Exception, _unexpected_error_handler)
    return app


def _resolve_loopback(
    host: str,
    port: int,
) -> tuple[tuple[int, int, int, str, tuple[Any, ...]], ...]:
    try:
        addresses = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
            flags=socket.AI_PASSIVE | socket.AI_NUMERICHOST,
        )
    except socket.gaierror as error:
        raise ConfigurationError(f"API host {host!r} could not be resolved") from error

    usable: list[tuple[int, int, int, str, tuple[Any, ...]]] = []
    for family, socket_type, protocol, canonical_name, address in addresses:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        address_text = str(address[0]).split("%", 1)[0]
        try:
            is_loopback = ipaddress.ip_address(address_text).is_loopback
        except ValueError as error:
            raise ConfigurationError(
                f"API host {host!r} did not resolve to an IP address"
            ) from error
        if not is_loopback:
            raise ConfigurationError(
                "non-loopback API binding requires an authenticated trusted "
                "deployment, which is not enabled by this launcher"
            )
        usable.append((family, socket_type, protocol, canonical_name, address))

    if not usable:
        raise ConfigurationError(f"API host {host!r} has no usable TCP address")
    return tuple(usable)


def _is_port_collision(error: OSError) -> bool:
    error_number = error.errno
    windows_error = getattr(error, "winerror", None)
    if error_number in (errno.EADDRINUSE, 10_048) or windows_error == 10_048:
        return True
    # With SO_EXCLUSIVEADDRUSE, Winsock may report WSAEACCES for an address
    # already owned by another socket. Do not classify POSIX EACCES this way.
    return windows_error == 10_013


def bind_server_socket(
    host: str = DEFAULT_API_HOST,
    port: int = DEFAULT_API_PORT,
) -> socket.socket:
    """Validate and reserve a loopback TCP listener for Uvicorn handoff."""

    validated_host = _validate_host(host)
    validated_port = _validate_port(port)
    address = _resolve_loopback(validated_host, validated_port)[0]
    family, socket_type, protocol, _canonical_name, socket_address = address

    listener = socket.socket(family, socket_type, protocol)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        try:
            listener.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_EXCLUSIVEADDRUSE,
                1,
            )
        except OSError:
            listener.close()
            raise

    try:
        listener.bind(socket_address)
        listener.listen(_SOCKET_BACKLOG)
        return listener
    except OSError as error:
        listener.close()
        if _is_port_collision(error):
            raise PortInUseError(validated_host, validated_port) from error
        raise


def run_server(
    manager: SyncManager,
    *,
    host: str = DEFAULT_API_HOST,
    port: int = DEFAULT_API_PORT,
    workers: int = 1,
    reload: bool = False,
    log_file: str = DEFAULT_API_LOG_FILE,
    log_level: str = DEFAULT_API_LOG_LEVEL,
    allow_unbounded_correction: bool = False,
) -> None:
    """Run one Uvicorn server using a retained, pre-bound loopback socket."""

    if manager is None:
        raise TypeError("manager is required")
    validated_host = _validate_host(host)
    validated_port = _validate_port(port)
    validated_log_file = _validate_log_file(log_file)
    validated_log_level = _validate_log_level(log_level)
    allow_unbounded = _validate_allow_unbounded_correction(allow_unbounded_correction)
    _validate_server_mode(workers, reload)
    if manager.snapshot().is_running:
        raise WorkerStateError(
            "the sync manager must be stopped before the API server starts"
        )
    if manager.config.max_correction is None and not allow_unbounded:
        raise ConfigurationError(
            "an unbounded manager requires explicit server authorization"
        )
    privilege_check = _detect_time_sync_privilege()
    log_path = configure_service_logging(validated_log_level, validated_log_file)
    logger.info(
        "management service starting version=%s host=%s port=%d log_file=%s "
        "allow_unbounded_correction=%s time_sync_privilege=%s",
        __version__,
        validated_host,
        validated_port,
        log_path,
        allow_unbounded,
        privilege_check.status.value,
    )
    try:
        listener = bind_server_socket(validated_host, validated_port)
    except PortInUseError:
        logger.error(
            "management service address is already in use host=%s port=%d",
            validated_host,
            validated_port,
        )
        raise
    except OSError as error:
        incident_id = uuid4().hex
        logger.exception(
            "management service socket setup failed "
            "incident_id=%s error_type=%s host=%s port=%d",
            incident_id,
            type(error).__name__,
            validated_host,
            validated_port,
        )
        raise

    logger.debug(
        "management service reserved TCP listener host=%s port=%d",
        validated_host,
        validated_port,
    )
    shutdown = UvicornShutdownAdapter()
    server_error: BaseException | None = None

    try:
        app = create_app(
            manager,
            shutdown.request_shutdown,
            allow_unbounded_correction=allow_unbounded,
            time_sync_privilege=privilege_check,
        )
        config = uvicorn.Config(
            app=app,
            host=validated_host,
            port=validated_port,
            workers=1,
            reload=False,
            log_level=validated_log_level,
        )
        server = uvicorn.Server(config)
        shutdown.attach(server)
        server.run(sockets=[listener])
        logger.info("management server loop completed")
    except KeyboardInterrupt as error:
        server_error = error
        logger.info("management service interrupted")
        raise
    except BaseException as error:
        server_error = error
        incident_id = uuid4().hex
        logger.exception(
            "management server failed incident_id=%s error_type=%s",
            incident_id,
            type(error).__name__,
        )
        raise
    finally:
        try:
            manager.stop()
        except Exception as error:
            if server_error is None:
                raise
            incident_id = uuid4().hex
            logger.exception(
                "manager cleanup also failed during server failure "
                "(incident_id=%s, error_type=%s)",
                incident_id,
                type(error).__name__,
            )
        finally:
            listener.close()
            logger.info("management service shutdown complete")


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run the same validated CLI path as the installed console command."""

    return _cli_main(argv, environ=environ, server_runner=run_server)


__all__ = [
    "CloseAuthorizer",
    "CloseResponse",
    "CycleRequest",
    "CycleResponse",
    "DEFAULT_API_HOST",
    "DEFAULT_API_LOG_FILE",
    "DEFAULT_API_LOG_LEVEL",
    "DEFAULT_API_PORT",
    "HelpParameter",
    "HelpResponse",
    "MAX_CYCLE_SECONDS",
    "MaxCorrectionRequest",
    "MaxCorrectionResponse",
    "OffsetResponse",
    "PingResponse",
    "PortInUseError",
    "RouteHelp",
    "RunningResponse",
    "StatusResponse",
    "SyncResponse",
    "ThreshRequest",
    "ThreshResponse",
    "TimestampResponse",
    "TimeSyncPrivilegeCheck",
    "TimeSyncPrivilegeStatus",
    "UvicornShutdownAdapter",
    "bind_server_socket",
    "create_app",
    "main",
    "run_server",
]


if __name__ == "__main__":
    raise SystemExit(main())
