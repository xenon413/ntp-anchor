"""NTP measurement collection, response validation, and consensus policy."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import math
from statistics import median
from typing import Any, Protocol

import ntplib

from .config import SyncConfig
from .exceptions import NTPQueryError, NTPResponseValidationError
from .models import NTPMeasurement, NTPQueryResult, NTPRejection


class NTPTransport(Protocol):
    """Narrow transport seam implemented by ``ntplib.NTPClient``."""

    def request(
        self,
        host: str,
        *,
        version: int,
        port: int,
        timeout: float,
    ) -> Any: ...


class NTPQueryClient(Protocol):
    """Measurement interface consumed by the synchronization manager."""

    def query(self, config: SyncConfig) -> NTPQueryResult: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _attribute(response: Any, name: str) -> Any:
    try:
        return getattr(response, name)
    except (AttributeError, TypeError) as error:
        raise NTPResponseValidationError(f"NTP response is missing {name}") from error


def _finite(response: Any, name: str) -> float:
    value = _attribute(response, name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NTPResponseValidationError(f"NTP response {name} is not numeric")
    result = float(value)
    if not math.isfinite(result):
        raise NTPResponseValidationError(f"NTP response {name} is not finite")
    return result


def _response_integer(response: Any, name: str) -> int:
    value = _attribute(response, name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise NTPResponseValidationError(f"NTP response {name} is not an integer")
    return value


class NTPClient:
    """Collect validated responses and derive an outlier-resistant offset.

    Every configured authority is queried once.  Invalid responses and query
    failures are retained as sanitized rejection metadata.  A result is
    returned only when at least ``config.quorum`` offsets remain after the
    median-deviation policy is applied.
    """

    def __init__(
        self,
        transport: NTPTransport | None = None,
        *,
        utc_now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._transport = transport if transport is not None else ntplib.NTPClient()
        self._utc_now = utc_now

    def query(self, config: SyncConfig) -> NTPQueryResult:
        measurements: list[NTPMeasurement] = []
        rejected: list[NTPRejection] = []

        for server in config.servers:
            try:
                response = self._transport.request(
                    server,
                    version=config.ntp_version,
                    port=config.ntp_port,
                    timeout=config.query_timeout,
                )
                measurements.append(self._validate(server, response, config))
            except NTPResponseValidationError as error:
                rejected.append(
                    NTPRejection(
                        server=server,
                        reason=str(error),
                        error_type=type(error).__name__,
                    )
                )
            except (ntplib.NTPException, OSError) as error:
                # Error text from socket/DNS libraries can contain local host
                # details.  Keep only a stable class name in structured state.
                rejected.append(
                    NTPRejection(
                        server=server,
                        reason="NTP query failed",
                        error_type=type(error).__name__,
                    )
                )

        if len(measurements) < config.quorum:
            raise self._query_error(config, rejected, len(measurements))

        center = float(median(item.offset for item in measurements))
        inliers: list[NTPMeasurement] = []
        for measurement in measurements:
            if abs(measurement.offset - center) <= config.max_sample_deviation:
                inliers.append(measurement)
            else:
                rejected.append(
                    NTPRejection(
                        server=measurement.server,
                        reason="offset is outside the configured consensus deviation",
                        error_type="NTPOutlier",
                    )
                )

        if len(inliers) < config.quorum:
            raise self._query_error(config, rejected, len(inliers))

        consensus_offset = float(median(item.offset for item in inliers))
        return NTPQueryResult(
            offset=consensus_offset,
            measurements=tuple(inliers),
            rejected=tuple(rejected),
            queried_at=self._aware_utc_now(),
        )

    def _validate(
        self,
        server: str,
        response: Any,
        config: SyncConfig,
    ) -> NTPMeasurement:
        offset = _finite(response, "offset")
        delay = _finite(response, "delay")
        root_delay = _finite(response, "root_delay")
        root_dispersion = _finite(response, "root_dispersion")

        if delay < 0.0 or delay > config.max_delay:
            raise NTPResponseValidationError(
                "NTP response delay exceeds the configured bound"
            )
        if abs(root_delay) > config.max_delay:
            raise NTPResponseValidationError(
                "NTP response root delay exceeds the configured bound"
            )
        if root_dispersion < 0.0 or root_dispersion > config.max_dispersion:
            raise NTPResponseValidationError(
                "NTP response root dispersion exceeds the configured bound"
            )

        leap = _response_integer(response, "leap")
        if leap not in (0, 1, 2):
            raise NTPResponseValidationError(
                "NTP server reports an unsynchronized leap state"
            )

        stratum = _response_integer(response, "stratum")
        if not 1 <= stratum <= config.max_stratum:
            raise NTPResponseValidationError("NTP response stratum is not synchronized")

        version = _response_integer(response, "version")
        if version != config.ntp_version:
            raise NTPResponseValidationError(
                "NTP response version does not match the request"
            )

        mode = _response_integer(response, "mode")
        if mode != 4:
            raise NTPResponseValidationError("NTP response is not in server mode")

        poll = _response_integer(response, "poll")
        precision = _response_integer(response, "precision")
        reference_id = _response_integer(response, "ref_id")
        reference_timestamp = _finite(response, "ref_time")
        origin_timestamp = _finite(response, "orig_time")
        receive_timestamp = _finite(response, "recv_time")
        transmit_timestamp = _finite(response, "tx_time")
        destination_timestamp = _finite(response, "dest_time")

        if reference_timestamp <= 0.0:
            raise NTPResponseValidationError(
                "NTP response reference timestamp is invalid"
            )
        if (
            min(
                origin_timestamp,
                receive_timestamp,
                transmit_timestamp,
                destination_timestamp,
            )
            <= 0.0
        ):
            raise NTPResponseValidationError(
                "NTP response contains an invalid exchange timestamp"
            )
        if transmit_timestamp < receive_timestamp:
            raise NTPResponseValidationError(
                "NTP server transmit time precedes receive time"
            )
        reference_age = transmit_timestamp - reference_timestamp
        if reference_age < 0.0 or reference_age > config.max_reference_age:
            raise NTPResponseValidationError(
                "NTP response reference timestamp is stale or in the future"
            )
        if destination_timestamp < origin_timestamp:
            raise NTPResponseValidationError(
                "NTP response destination time precedes origin time"
            )

        return NTPMeasurement(
            server=server,
            offset=offset,
            delay=delay,
            root_delay=root_delay,
            root_dispersion=root_dispersion,
            stratum=stratum,
            leap=leap,
            version=version,
            mode=mode,
            poll=poll,
            precision=precision,
            reference_id=reference_id,
            reference_timestamp=reference_timestamp,
            origin_timestamp=origin_timestamp,
            receive_timestamp=receive_timestamp,
            transmit_timestamp=transmit_timestamp,
            destination_timestamp=destination_timestamp,
            received_at=self._aware_utc_now(),
        )

    def _aware_utc_now(self) -> datetime:
        value = self._utc_now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("utc_now must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _query_error(
        config: SyncConfig,
        rejected: list[NTPRejection],
        received: int,
    ) -> NTPQueryError:
        failures = {
            rejection.server: f"{rejection.error_type}: {rejection.reason}"
            for rejection in rejected
        }
        return NTPQueryError(
            config.servers,
            failures,
            required=config.quorum,
            received=received,
        )


__all__ = ["NTPClient", "NTPQueryClient", "NTPTransport"]
