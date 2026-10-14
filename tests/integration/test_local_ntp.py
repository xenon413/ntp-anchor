from __future__ import annotations

from dataclasses import dataclass, field
import socket
import struct
import threading
import time

import pytest

from ntp_anchor.core.config import SyncConfig
from ntp_anchor.core.exceptions import NTPQueryError
from ntp_anchor.core.ntp_client import NTPClient


pytestmark = pytest.mark.integration

_NTP_EPOCH_DELTA = 2_208_988_800


def _ntp_timestamp(unix_timestamp: float) -> bytes:
    value = unix_timestamp + _NTP_EPOCH_DELTA
    seconds = int(value)
    fraction = int((value - seconds) * 2**32) & 0xFFFFFFFF
    return struct.pack("!II", seconds & 0xFFFFFFFF, fraction)


@dataclass
class LocalNTPResponder:
    mode: int = 4
    _socket: socket.socket = field(init=False, repr=False)
    _thread: threading.Thread = field(init=False, repr=False)
    _errors: list[BaseException] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.settimeout(2.0)
        self._thread = threading.Thread(
            target=self._serve_once,
            name="test-local-ntp-responder",
            daemon=True,
        )

    @property
    def port(self) -> int:
        return int(self._socket.getsockname()[1])

    def __enter__(self) -> LocalNTPResponder:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._thread.join(3.0)
        self._socket.close()
        if self._thread.is_alive():
            raise AssertionError("local NTP responder did not finish")
        if self._errors:
            raise self._errors[0]

    def _serve_once(self) -> None:
        try:
            request, peer = self._socket.recvfrom(512)
            if len(request) < 48:
                raise AssertionError("NTP request was shorter than 48 bytes")
            version = (request[0] >> 3) & 0b111
            first_byte = (version << 3) | self.mode
            received = time.time()
            transmitted = time.time()
            packet = b"".join(
                (
                    struct.pack("!BBBb", first_byte, 2, 6, -20),
                    struct.pack("!II", int(0.001 * 2**16), int(0.001 * 2**16)),
                    b"LOCL",
                    _ntp_timestamp(received - 1.0),
                    request[40:48],
                    _ntp_timestamp(received),
                    _ntp_timestamp(transmitted),
                )
            )
            if len(packet) != 48:
                raise AssertionError(f"invalid test NTP packet length: {len(packet)}")
            self._socket.sendto(packet, peer)
        except BaseException as error:
            self._errors.append(error)


def _config(port: int) -> SyncConfig:
    return SyncConfig(
        servers=("127.0.0.1",),
        ntp_port=port,
        quorum=1,
        query_timeout=1.0,
        max_delay=1.0,
        max_dispersion=1.0,
    )


def test_real_udp_packet_round_trip_is_validated_locally() -> None:
    with LocalNTPResponder() as responder:
        result = NTPClient().query(_config(responder.port))

    assert result.servers == ("127.0.0.1",)
    assert abs(result.offset) < 0.1
    assert result.measurements[0].mode == 4
    assert result.measurements[0].stratum == 2
    assert result.rejected == ()


def test_invalid_local_ntp_packet_is_rejected() -> None:
    with LocalNTPResponder(mode=3) as responder:
        with pytest.raises(NTPQueryError) as caught:
            NTPClient().query(_config(responder.port))

    assert caught.value.received == 0
    assert "NTPResponseValidationError" in caught.value.failures["127.0.0.1"]


def test_ntp_udp_port_is_independent_from_same_numbered_tcp_listener() -> None:
    with LocalNTPResponder() as responder:
        tcp_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp_listener.bind(("127.0.0.1", responder.port))
        tcp_listener.listen(1)
        try:
            result = NTPClient().query(_config(responder.port))
        finally:
            tcp_listener.close()

    assert result.servers == ("127.0.0.1",)
