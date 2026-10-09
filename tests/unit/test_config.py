import math

import pytest

from ntp_anchor.core.config import SyncConfig, _finite_float, _finite_int
from ntp_anchor.core.exceptions import ConfigurationError


@pytest.mark.unit
@pytest.mark.parametrize(
    "servers",
    [
        None,
        "pool.ntp.org",
        (),
        ("",),
        (" pool.ntp.org",),
        ("pool.ntp.org", "POOL.NTP.ORG"),
        (123,),
    ],
)
def test_invalid_server_collections_are_rejected(servers: object) -> None:
    with pytest.raises(ConfigurationError):
        SyncConfig(servers=servers)  # type: ignore[arg-type]


@pytest.mark.unit
def test_equivalent_ipv6_addresses_are_duplicates() -> None:
    with pytest.raises(ConfigurationError):
        SyncConfig(
            servers=("2001:db8::1", "2001:0db8:0:0:0:0:0:1"),
            quorum=1,
        )


@pytest.mark.unit
@pytest.mark.parametrize("port", [0, 65_536, -1, True, 123.0, "123"])
def test_invalid_ntp_ports_are_rejected(port: object) -> None:
    with pytest.raises(ConfigurationError):
        SyncConfig(ntp_port=port)  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cycle", 0.0),
        ("cycle", -1.0),
        ("cycle", math.inf),
        ("thresh", -0.1),
        ("thresh", math.nan),
        ("query_timeout", 0.0),
        ("command_timeout", math.inf),
        ("max_reference_age", -1.0),
        ("max_correction", -1.0),
        ("max_correction", math.inf),
        ("max_correction", True),
        ("retry_jitter", 1.1),
    ],
)
def test_invalid_numeric_configuration_is_rejected(field: str, value: object) -> None:
    with pytest.raises(ConfigurationError):
        SyncConfig(**{field: value})  # type: ignore[arg-type]


@pytest.mark.unit
def test_threshold_cannot_exceed_maximum_correction() -> None:
    with pytest.raises(ConfigurationError):
        SyncConfig(thresh=5.1, max_correction=5.0)


@pytest.mark.unit
def test_none_disables_the_maximum_correction_guard_in_core_config() -> None:
    config = SyncConfig(thresh=50.0, max_correction=None)

    assert config.max_correction is None
    assert config.with_max_correction(75.0).max_correction == pytest.approx(75.0)


@pytest.mark.unit
def test_one_server_requires_explicit_single_server_quorum() -> None:
    with pytest.raises(ConfigurationError):
        SyncConfig(servers=("time.example.com",))

    config = SyncConfig(servers=("time.example.com",), quorum=1)
    assert config.quorum == 1


@pytest.mark.unit
def test_server_count_is_bounded() -> None:
    servers = tuple(f"time-{index}.example.com" for index in range(17))

    with pytest.raises(ConfigurationError):
        SyncConfig(servers=servers, quorum=1)


@pytest.mark.unit
def test_numeric_helpers_allow_omitting_the_field_name() -> None:
    assert _finite_float(1.5, minimum=0.0) == 1.5
    assert _finite_int(3, minimum=1) is None

    with pytest.raises(ConfigurationError, match="value must be finite"):
        _finite_float(math.nan, minimum=0.0)


@pytest.mark.unit
def test_finite_float_enforces_an_optional_inclusive_maximum() -> None:
    assert (
        _finite_float(
            1.0,
            minimum=0.0,
            maximum=1.0,
        )
        == 1.0
    )

    with pytest.raises(ConfigurationError, match=r"value must be <= 1\.0"):
        _finite_float(
            1.01,
            minimum=0.0,
            maximum=1.0,
        )


@pytest.mark.unit
def test_finite_int_supports_exclusive_bounds() -> None:
    assert _finite_int(2, minimum=1, maximum=3, inclusive=False) is None

    with pytest.raises(ConfigurationError, match="value must be > 1"):
        _finite_int(1, minimum=1, maximum=3, inclusive=False)

    with pytest.raises(ConfigurationError, match="value must be < 3"):
        _finite_int(3, minimum=1, maximum=3, inclusive=False)
