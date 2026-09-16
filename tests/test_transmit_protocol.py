import json

import pytest

from js8mail.adapters.js8call.protocol import ApiProtocolError, encode_transmit_request


def test_transmit_request_is_bounded_json_line() -> None:
    packet = json.loads(encode_transmit_request("TX.SET_TEXT", "N0CALL hello", request_id="1"))
    assert packet == {"params": {"_ID": "1"}, "type": "TX.SET_TEXT", "value": "N0CALL hello"}


def test_transmit_request_rejects_wrong_action_or_size() -> None:
    with pytest.raises(ApiProtocolError):
        encode_transmit_request("PING", "x", request_id="1")
    with pytest.raises(ApiProtocolError):
        encode_transmit_request("TX.SET_TEXT", "x" * 4097, request_id="1")
