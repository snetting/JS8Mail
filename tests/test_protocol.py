import pytest

from js8mail.adapters.js8call.protocol import (
    ApiProtocolError,
    decode_line,
    encode_control_request,
    encode_read_only_request,
    encode_speed_request,
    normalize_directed_event,
    parse_legacy_ack,
)
from js8mail.domain import NormalizedEvent
from js8mail.protocol import (
    MultipartError,
    contains_js8mail_marker,
    find_capability,
    format_capability,
    format_delivery_ack,
    parse_ack,
    parse_capability,
    parse_delivery_ack,
)


def test_decode_valid_event() -> None:
    message = decode_line(b'{"type":"RX.DIRECTED","value":"N0CALL: HI","params":{"SNR":-10}}')
    assert message.type == "RX.DIRECTED"
    assert message.params["SNR"] == -10


def test_capability_exchange_is_canonical_and_marker_is_only_a_hint() -> None:
    assert format_capability(("mp", "E2E", "mp")) == "J8M1 CAP 1 MP,E2E"
    assert parse_capability("j8m1 cap 1 mp,e2e") == (1, ("MP", "E2E"))
    assert contains_js8mail_marker("N0CALL MSG [JS8Mail/0.0.5] hello")
    assert not contains_js8mail_marker("N0CALL MSG JS8Mail hello")
    assert find_capability("M0OUE: OH3SPN J8M1 CAP 1 E2E,MP,PA ♢") == (
        1, ("E2E", "MP", "PA")
    )


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
    assert b'"type":"RIG.TX_HALT"' in encode_control_request("RIG.TX_HALT", request_id="halt-1")
    with pytest.raises(ApiProtocolError):
        encode_speed_request(9, request_id="speed-2")


def test_capability_advertisement_is_versioned_and_bounded() -> None:
    encoded = format_capability()
    assert encoded == "J8M1 CAP 1 E2E,MP,PA"
    assert parse_capability(encoded) == (1, ("E2E", "MP", "PA"))
    assert parse_capability("J8M1 CAP 1 E2E,UNKNOWN") is None
    assert parse_capability("J8M1 CAP 2 E2E,MP,PA") is None


def test_only_complete_receipts_are_acknowledgements() -> None:
    assert parse_ack("J8M1 DELIVERED abc 123 ?") == ("delivered", "abc", None)
    assert parse_ack("J8M1 DELIVERED abc not-a-time ?") is None
    assert parse_ack("J8M1 DELIVERED abc 123") is None


def test_enhanced_metadata_rejects_injected_ids_and_paths() -> None:
    assert parse_delivery_ack("J8M1 DELIVERED bad/id 123 ?") is None
    assert parse_delivery_ack("J8M1 DELIVERED abc 123 OH3SPN,not a call") is None
    with pytest.raises(MultipartError):
        format_delivery_ack("abc", 123, ("OH3SPN", "bad call"))


def test_normalize_real_js8call_directed_text() -> None:
    event = NormalizedEvent(
        "RX.DIRECTED",
        "OH3SPN MSG TO:M0SFW HELLO ♢",
        {
            "FROM": "M7XNT",
            "TO": "OH3SPN",
            "CMD": " MSG TO:",
            "TEXT": "OH3SPN MSG TO:M0SFW HELLO ♢",
        },
        1,
    )
    frame = normalize_directed_event(event)
    assert frame is not None
    assert frame.command == "MSG TO:"
    assert frame.payload == "HELLO"
    assert frame.stored_recipient == "M0SFW"


def test_normalize_envelope_after_js8call_msg_wrapper() -> None:
    event = NormalizedEvent(
        "RX.DIRECTED",
        "OH3SPN MSG J8M1 D abc 1/2 readable ♢",
        {
            "FROM": "RELAY1",
            "TO": "OH3SPN",
            "CMD": " MSG",
            "TEXT": "OH3SPN MSG J8M1 D abc 1/2 readable ♢",
        },
        1,
    )
    frame = normalize_directed_event(event)
    assert frame is not None
    assert frame.payload == "J8M1 D abc 1/2 readable"


def test_parse_direct_and_relayed_legacy_ack() -> None:
    direct = normalize_directed_event(
        NormalizedEvent(
            "RX.DIRECTED", "OH3SPN ACK", {"FROM": "MM0ZFG", "TO": "OH3SPN", "CMD": " ACK"}, 1
        )
    )
    assert direct is not None
    assert parse_legacy_ack(direct) == ("MM0ZFG", ("MM0ZFG",))

    relayed = normalize_directed_event(
        NormalizedEvent(
            "RX.DIRECTED",
            "OH3SPN> ACK *DE* MM0ZFG",
            {
                "FROM": "IZ1KJG",
                "TO": "OH3SPN",
                "CMD": ">",
                "TEXT": "OH3SPN> ACK *DE* MM0ZFG",
            },
            1,
        )
    )
    assert relayed is not None
    assert parse_legacy_ack(relayed) == ("MM0ZFG", ("OH3SPN", "MM0ZFG"))

    # An ordinary relayed text is not an ACK and must not complete mail.
    assert parse_legacy_ack(
        normalize_directed_event(
            NormalizedEvent(
                "RX.DIRECTED", "OH3SPN> MM0ZFG>J8M1 CAP 1 E2E", {
                    "FROM": "IZ1KJG", "TO": "OH3SPN", "CMD": ">"
                }, 1
            )
        )
    ) is None
