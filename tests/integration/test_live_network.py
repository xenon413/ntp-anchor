from __future__ import annotations

import pytest

from ntp_anchor.core.config import SyncConfig
from ntp_anchor.core.ntp_client import NTPClient


pytestmark = [pytest.mark.integration, pytest.mark.live_network]


def test_public_ntp_quorum_read_only() -> None:
    """Exercise real NTP measurement only; this test never requests correction."""

    config = SyncConfig(query_timeout=5.0)

    result = NTPClient().query(config)

    assert len(result.measurements) >= config.quorum
    assert set(result.servers).issubset(config.servers)

