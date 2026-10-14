from __future__ import annotations

import os

import pytest

from ntp_anchor.core.config import SyncConfig
from ntp_anchor.core.core_logic import create_sync_manager
from ntp_anchor.core.models import CheckStatus


pytestmark = [pytest.mark.integration, pytest.mark.privileged]

_ACKNOWLEDGEMENT = "I_UNDERSTAND_THIS_CHANGES_SYSTEM_TIME"
_EXECUTE_ENV = "NTP_ANCHOR_EXECUTE_CLOCK_MUTATION"


def test_privileged_gate_is_open_only_on_acknowledged_disposable_host() -> None:
    assert os.environ["NTP_ANCHOR_DISPOSABLE_HOST"] == "1"
    assert os.environ["NTP_ANCHOR_RUN_PRIVILEGED"] == _ACKNOWLEDGEMENT


@pytest.mark.live_network
def test_real_clock_sync_and_post_verification_on_disposable_host() -> None:
    """Mutate the host clock only after every collection and runtime guard passes."""

    if os.environ.get(_EXECUTE_ENV) != _ACKNOWLEDGEMENT:
        pytest.skip(
            f"real clock mutation additionally requires {_EXECUTE_ENV}="
            f"{_ACKNOWLEDGEMENT}"
        )

    config = SyncConfig(
        thresh=0.0,
        max_correction=5.0,
        query_timeout=5.0,
        command_timeout=15.0,
        verification_attempts=3,
        verification_interval=0.5,
        stop_timeout=90.0,
    )
    manager = create_sync_manager(config)

    result = manager.check_once()

    assert result.status in {
        CheckStatus.WITHIN_THRESHOLD,
        CheckStatus.SYNC_SUCCEEDED,
    }
    if result.correction_attempted:
        assert result.status is CheckStatus.SYNC_SUCCEEDED
        assert result.verification is not None
        assert result.synced_at is not None
