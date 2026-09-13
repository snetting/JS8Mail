import pytest

from js8mail.adapters.js8call.protocol import (
    ApiProtocolError,
    decode_line,
    encode_read_only_request,
    encode_speed_request,
)
from js8mail.protocol import format_capability, parse_capability


def test_decode_valid_event() -> None:
    message = decode_line(b'{"type":"RX.DIRECTED","value":"N0CALL: HI","params":{"SNR":-10}}')
    assert message.type == "RX.DIRECTED"
    assert message.params["SNR"] == -10


def test_decode_rejects_malformed_and_oversized_frames() -> None:
    with pytest.raises(ApiProtocolError):
        decode_line(b"not json")
    with pytest.raises(ApiProtocolError):
        decode_line(b'{"type":"RX.ACTIVITY","value":"' + b"x" * 20_000 + b'"}')


def test_request_encoder_is_receive_only() -> None:
    request = encode_read_only_request("STATION.GET_STATUS", request_id="probe-1")
    assert b"STATION.GET_STATUS" in request
    with pytest.raises(ApiProtocolError):
        encode_read_only_request("TX.SEND_MESSAGE", request_id="no-send")
    assert b'"type":"MODE.SET_SPEED"' in encode_speed_request(2, request_id="speed-1")
    with pytest.raises(ApiProtocolError):
        encode_speed_request(9, request_id="speed-2")


def test_capability_advertisement_is_versioned_and_bounded() -> None:
    encoded = format_capability()
    assert encoded == "J8M1 CAP 1 E2E,MP,PA"
    assert parse_capability(encoded) == (1, ("E2E", "MP", "PA"))
    assert parse_capability("J8M1 CAP 1 E2E,UNKNOWN") is None
