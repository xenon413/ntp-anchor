from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ntp_anchor.core.config import SyncConfig
from ntp_anchor.core.exceptions import NTPQueryError
from ntp_anchor.core.ntp_client import NTPClient


def _response(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "offset": 0.1,
        "delay": 0.02,
        "root_delay": 0.01,
        "root_dispersion": 0.01,
        "stratum": 2,
        "leap": 0,
        "version": 3,
        "mode": 4,
        "poll": 6,
        "precision": -20,
        "ref_id": 1,
        "ref_time": 1_700_000_000.0,
        "orig_time": 1_700_000_001.0,
        "recv_time": 1_700_000_001.01,
        "tx_time": 1_700_000_001.02,
        "dest_time": 1_700_000_001.03,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeTransport:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, int, int, float]] = []

    def request(
        self,
        host: str,
        *,
        version: int,
        port: int,
        timeout: float,
    ) -> object:
        self.calls.append((host, version, port, timeout))
        response = self.responses[host]
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.mark.unit
def test_query_uses_quorum_median_and_rejects_outlier() -> None:
    transport = FakeTransport(
        {
            "a.example.com": _response(offset=0.10),
            "b.example.com": _response(offset=0.12),
            "c.example.com": _response(offset=2.00),
        }
    )
    client = NTPClient(
        transport,
        utc_now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    config = SyncConfig(
        servers=("a.example.com", "b.example.com", "c.example.com"),
        quorum=2,
        max_sample_deviation=0.2,
    )

    result = client.query(config)

    assert result.offset == pytest.approx(0.11)
    assert result.servers == ("a.example.com", "b.example.com")
    assert [(item.server, item.error_type) for item in result.rejected] == [
        ("c.example.com", "NTPOutlier")
    ]
    assert transport.calls == [
        ("a.example.com", 3, 123, 2.0),
        ("b.example.com", 3, 123, 2.0),
        ("c.example.com", 3, 123, 2.0),
    ]


@pytest.mark.unit
def test_query_reports_required_and_received_when_quorum_fails() -> None:
    transport = FakeTransport(
        {
            "a.example.com": _response(offset=0.1, leap=3),
            "b.example.com": TimeoutError("network detail must not leak"),
        }
    )
    client = NTPClient(transport)
    config = SyncConfig(
        servers=("a.example.com", "b.example.com"),
        quorum=2,
    )

    with pytest.raises(NTPQueryError) as caught:
        client.query(config)

    assert caught.value.required == 2
    assert caught.value.received == 0
    assert set(caught.value.failures) == {"a.example.com", "b.example.com"}
    assert "network detail must not leak" not in str(caught.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", 3),
        ("stratum", 16),
        ("delay", 2.0),
        ("root_dispersion", float("inf")),
        ("ref_time", 0.0),
        ("ref_time", 1_600_000_000.0),
        ("offset", float("nan")),
    ],
)
def test_invalid_response_metadata_is_rejected(field: str, value: object) -> None:
    transport = FakeTransport({"a.example.com": _response(**{field: value})})
    client = NTPClient(transport)
    config = SyncConfig(servers=("a.example.com",), quorum=1)

    with pytest.raises(NTPQueryError) as caught:
        client.query(config)

    assert caught.value.received == 0
    assert "NTPResponseValidationError" in caught.value.failures["a.example.com"]


@pytest.mark.unit
def test_per_server_transport_failure_is_sanitized_without_losing_consensus() -> None:
    transport = FakeTransport(
        {
            "good.example.com": _response(offset=0.1),
            "failed.example.com": OSError(
                "private DNS detail for workstation.internal"
            ),
        }
    )
    client = NTPClient(transport)
    config = SyncConfig(
        servers=("good.example.com", "failed.example.com"),
        quorum=1,
    )

    result = client.query(config)

    assert result.offset == pytest.approx(0.1)
    assert result.servers == ("good.example.com",)
    assert [
        (item.server, item.reason, item.error_type) for item in result.rejected
    ] == [
        ("failed.example.com", "NTP query failed", "OSError")
    ]
    assert "workstation.internal" not in repr(result.rejected)


@pytest.mark.unit
def test_even_consensus_uses_statistical_median() -> None:
    transport = FakeTransport(
        {
            "a.example.com": _response(offset=-0.2),
            "b.example.com": _response(offset=0.2),
        }
    )
    result = NTPClient(transport).query(
        SyncConfig(
            servers=("a.example.com", "b.example.com"),
            quorum=2,
            max_sample_deviation=0.2,
        )
    )

    assert result.offset == pytest.approx(0.0)


@pytest.mark.unit
def test_outlier_rejection_that_breaks_quorum_is_a_query_failure() -> None:
    transport = FakeTransport(
        {
            "a.example.com": _response(offset=0.0),
            "b.example.com": _response(offset=1.0),
            "c.example.com": _response(offset=2.0),
        }
    )
    config = SyncConfig(
        servers=("a.example.com", "b.example.com", "c.example.com"),
        quorum=2,
        max_sample_deviation=0.1,
    )

    with pytest.raises(NTPQueryError) as caught:
        NTPClient(transport).query(config)

    assert caught.value.required == 2
    assert caught.value.received == 1
    assert set(caught.value.failures) == {"a.example.com", "c.example.com"}


@pytest.mark.unit
@pytest.mark.parametrize(
    "overrides",
    [
        {"delay": -0.001},
        {"root_delay": 1.001},
        {"root_dispersion": -0.001},
        {"leap": 3},
        {"stratum": 0},
        {"stratum": 16},
        {"version": 4},
        {"tx_time": 1_700_000_000.0, "recv_time": 1_700_000_001.0},
        {"dest_time": 1_700_000_000.0, "orig_time": 1_700_000_001.0},
    ],
)
def test_response_quality_policy_rejects_each_invalid_boundary(
    overrides: dict[str, object],
) -> None:
    transport = FakeTransport({"time.example.com": _response(**overrides)})

    with pytest.raises(NTPQueryError):
        NTPClient(transport).query(
            SyncConfig(servers=("time.example.com",), quorum=1)
        )


@pytest.mark.unit
def test_response_quality_accepts_configured_inclusive_bounds() -> None:
    transport = FakeTransport(
        {
            "time.example.com": _response(
                delay=1.0,
                root_delay=-1.0,
                root_dispersion=1.0,
                stratum=15,
            )
        }
    )

    result = NTPClient(transport).query(
        SyncConfig(servers=("time.example.com",), quorum=1)
    )

    assert result.servers == ("time.example.com",)
