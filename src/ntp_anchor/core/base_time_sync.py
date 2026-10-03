"""Platform backend contract for privileged clock operations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from .config import DEFAULT_NTP_SERVERS
from .exceptions import ConfigurationError


def normalize_sync_servers(
    servers: Sequence[str] | None,
    *,
    default: Sequence[str] = DEFAULT_NTP_SERVERS,
) -> tuple[str, ...]:
    """Validate server arguments at the final privileged-operation boundary."""

    selected = default if servers is None else servers
    if isinstance(selected, (str, bytes)):
        raise ConfigurationError("servers must be a non-empty sequence of host names or IP addresses")

    normalized: list[str] = []
    for server in selected:
        if not isinstance(server, str):
            raise ConfigurationError("every NTP server must be a string")
        if not server or server != server.strip() or any(character.isspace() for character in server):
            raise ConfigurationError("NTP server names must be non-empty and cannot contain whitespace")
        if any(ord(character) < 32 or ord(character) == 127 for character in server):
            raise ConfigurationError("NTP server names cannot contain control characters")
        normalized.append(server)

    if not normalized:
        raise ConfigurationError("at least one NTP server is required")
    return tuple(normalized)


class BaseTimeSync(ABC):
    """Interface implemented by an injected, platform-specific backend."""

    def prepare_time_sync(self, servers: Sequence[str]) -> None:
        """Prepare for a correction using *servers* as trusted authorities.

        Backends with a standalone one-shot client should override this hook
        so preparation does not start a separately configured clock daemon.
        The default preserves compatibility for service-backed adapters.
        """

        normalize_sync_servers(servers)
        self.start_time_service()

    @abstractmethod
    def start_time_service(self) -> None:
        """Ensure an eligible platform time service is ready."""

    @abstractmethod
    def sync_time(self, servers: Sequence[str] | None = None) -> None:
        """Request correction using the configured trust authorities."""
