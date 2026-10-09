"""Validated, immutable configuration for NTP synchronization.

This module deliberately has no platform imports or runtime side effects.  A
configuration can therefore be created and validated on every supported
Python platform, even when no clock-changing backend is available.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import ipaddress
import math
import re
from collections.abc import Iterable
from typing import Any

from .exceptions import ConfigurationError


DEFAULT_NTP_SERVERS = (
    "pool.ntp.org",
    "time.google.com",
    "time.cloudflare.com",
)

_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_MAX_NTP_SERVERS = 16


def _finite_float(
    value: Any,
    *,
    minimum: float,
    inclusive: bool = True,
    maximum: float | None = None,
    name: str | None = None,
) -> float:
    # NOTE: inclusive applies to both the minimum and maximum bounds.
    label = name or "value"

    # bool is an instance of int, so exclude it explicitly.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{label} must be a finite number")

    # Converting an extremely large integer to float can overflow.
    try:
        result = float(value)
    except OverflowError as exc:
        raise ConfigurationError(
            f"{label} is too large to represent as a float"
        ) from exc

    # Reject NaN and infinities.
    if not math.isfinite(result):
        raise ConfigurationError(f"{label} must be finite")

    # Validate the minimum bound.
    valid = result >= minimum if inclusive else result > minimum
    if not valid:
        operator = ">=" if inclusive else ">"
        raise ConfigurationError(f"{label} must be {operator} {minimum}")

    # Validate the maximum bound when one is configured.
    if maximum is not None:
        valid = result <= maximum if inclusive else result < maximum
        if not valid:
            operator = "<=" if inclusive else "<"
            raise ConfigurationError(f"{label} must be {operator} {maximum}")

    return result


def _finite_int(
    value: Any,
    *,
    minimum: int,
    maximum: int | None = None,
    inclusive: bool = True,
    name: str | None = None,
) -> None:
    # NOTE: inclusive applies to both the minimum and maximum bounds.
    label = name or "value"

    # bool is an instance of int, so exclude it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{label} must be an integer")

    # Validate the minimum bound.
    valid = value >= minimum if inclusive else value > minimum
    if not valid:
        operator = ">=" if inclusive else ">"
        raise ConfigurationError(f"{label} must be {operator} {minimum}")

    # Validate the maximum bound when one is configured.
    if maximum is not None:
        valid = value <= maximum if inclusive else value < maximum
        if not valid:
            operator = "<=" if inclusive else "<"
            raise ConfigurationError(f"{label} must be {operator} {maximum}")


def _validate_hostname(value: str) -> str:
    server = value.strip()

    # Validate the server format.
    if not server:
        raise ConfigurationError("NTP server names cannot be empty")
    if server != value:
        raise ConfigurationError("NTP server names cannot have surrounding whitespace")
    if any(character.isspace() or ord(character) < 32 for character in server):
        raise ConfigurationError(f"invalid NTP server name: {server!r}")
    if "://" in server or "/" in server or "@" in server:
        raise ConfigurationError(
            "NTP server must be a hostname or IP address without a scheme or "
            f"path: {server!r}"
        )
    if server.startswith("[") and server.endswith("]"):
        raise ConfigurationError(
            f"NTP server addresses must not use URI brackets: {server!r}"
        )

    try:
        address = ipaddress.ip_address(server)
    except ValueError:
        hostname = server[:-1] if server.endswith(".") else server
        if len(hostname) > 253 or not hostname:
            raise ConfigurationError(f"invalid NTP hostname: {server!r}")
        labels = hostname.split(".")

        # Reject labels that do not follow ASCII hostname rules.
        if any(not _HOST_LABEL.fullmatch(label) for label in labels):
            raise ConfigurationError(f"invalid NTP hostname: {server!r}")
        return hostname.lower()

    return str(address)


def _servers(value: Any) -> tuple[str, ...]:
    if (
        value is None
        or isinstance(value, (str, bytes))
        or not isinstance(value, Iterable)
    ):
        raise ConfigurationError(
            "servers must be a non-empty iterable of hostnames/IP addresses"
        )

    normalized: list[str] = []
    for server in value:
        if len(normalized) >= _MAX_NTP_SERVERS:
            raise ConfigurationError(
                f"servers cannot contain more than {_MAX_NTP_SERVERS} entries"
            )
        if not isinstance(server, str):
            raise ConfigurationError("every NTP server must be a string")

        normalized.append(_validate_hostname(server))

    if not normalized:
        raise ConfigurationError("servers must contain at least one NTP server")
    if len(set(normalized)) != len(normalized):
        raise ConfigurationError("servers must not contain duplicates")
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class SyncConfig:
    """All policy and scheduling inputs used by :class:`SyncManager`.

    The default three-server configuration requires two agreeing responses. A
    constrained single-server deployment must opt in explicitly with
    ``quorum=1``; duplicates can never be used to satisfy a quorum.
    """

    servers: tuple[str, ...] = DEFAULT_NTP_SERVERS
    ntp_port: int = 123
    ntp_version: int = 3
    thresh: float = 0.5
    cycle: float = 300.0
    query_timeout: float = 2.0
    quorum: int = 2
    max_delay: float = 1.0
    max_dispersion: float = 1.0
    max_reference_age: float = 86_400.0
    max_sample_deviation: float = 0.25
    max_correction: float | None = 5.0
    max_stratum: int = 15
    verification_attempts: int = 3
    verification_interval: float = 0.25
    auto_start_service: bool = True
    command_timeout: float = 10.0
    retry_base_delay: float = 1.0
    retry_max_delay: float = 60.0
    retry_jitter: float = 0.2
    single_flight_timeout: float = 5.0
    # Covers the default query/prepare/sync/verification budget. Callers may
    # choose a shorter deadline, in which case stop() reports a typed timeout
    # and continues to expose the still-live worker truthfully.
    stop_timeout: float = 90.0

    def __post_init__(self) -> None:
        normalized_servers = _servers(self.servers)

        # Validate integer fields without changing their values.
        for field_name, minimum, maximum in (
            ("ntp_port", 1, 65_535),
            ("ntp_version", 3, 4),
            ("quorum", 1, len(normalized_servers)),
            ("max_stratum", 1, 15),
            ("verification_attempts", 1, None),
        ):
            _finite_int(
                getattr(self, field_name),
                name=field_name,
                minimum=minimum,
                maximum=maximum,
            )

        # Validate and normalize floating-point fields.
        for field_name, minimum, maximum, inclusive in (
            ("thresh", 0.0, None, True),
            ("cycle", 0.0, None, False),
            ("query_timeout", 0.0, None, False),
            ("max_delay", 0.0, None, True),
            ("max_dispersion", 0.0, None, True),
            ("max_reference_age", 0.0, None, True),
            ("max_sample_deviation", 0.0, None, True),
            ("verification_interval", 0.0, None, True),
            ("command_timeout", 0.0, None, False),
            ("retry_base_delay", 0.0, None, False),
            ("retry_max_delay", 0.0, None, False),
            ("retry_jitter", 0.0, 1.0, True),
            ("single_flight_timeout", 0.0, None, False),
            ("stop_timeout", 0.0, None, False),
        ):
            object.__setattr__(
                self,
                field_name,
                _finite_float(
                    getattr(self, field_name),
                    name=field_name,
                    minimum=minimum,
                    maximum=maximum,
                    inclusive=inclusive,
                ),
            )

        # Store normalized servers and enforce the remaining invariants.
        object.__setattr__(self, "servers", normalized_servers)

        if self.max_correction is not None:
            object.__setattr__(
                self,
                "max_correction",
                _finite_float(
                    self.max_correction,
                    name="max_correction",
                    minimum=0.0,
                ),
            )

        if not isinstance(self.auto_start_service, bool):
            raise ConfigurationError("auto_start_service must be a Boolean")

        if self.max_correction is not None and self.max_correction < self.thresh:
            raise ConfigurationError(
                "max_correction must be greater than or equal to thresh"
            )
        if self.retry_max_delay < self.retry_base_delay:
            raise ConfigurationError(
                "retry_max_delay must be greater than or equal to retry_base_delay"
            )

    # Immutable update helpers.
    def with_cycle(self, seconds: float) -> SyncConfig:
        """Return a validated copy with a new scheduling interval."""

        return replace(self, cycle=seconds)

    def with_thresh(self, seconds: float) -> SyncConfig:
        """Return a validated copy with a new drift threshold."""

        return replace(self, thresh=seconds)

    def with_max_correction(self, seconds: float | None) -> SyncConfig:
        """Return a validated copy with a new optional correction limit."""

        return replace(self, max_correction=seconds)


__all__ = ["DEFAULT_NTP_SERVERS", "SyncConfig"]
