"""Bounded JSON-line protocol handling for JS8Call's local API.

This module deliberately exposes only read-only request construction. Sending RF
will be added behind a separately reviewed adapter capability.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MAX_LINE_BYTES = 16_384
MAX_VALUE_BYTES = 4_096
MAX_PARAMS = 128

READ_ONLY_REQUESTS = frozenset(
    {
        "PING",
        "RIG.GET_FREQ",
        "RIG.GET_PTT",
        "STATION.GET_CALLSIGN",
        "STATION.GET_GRID",
        "STATION.GET_INFO",
        "STATION.GET_STATUS",
        "STATION.VERSION",
        "STATION.GET_OS",
        "RX.GET_CALL_ACTIVITY",
        "RX.GET_CALL_SELECTED",
        "RX.GET_BAND_ACTIVITY",
        "RX.GET_TEXT",
        "RX.GET_FREE_OFFSETS",
        "TX.GET_TEXT",
        "TX.GET_QUEUE_DEPTH",
        "MODE.GET_SPEED",
        "INBOX.GET_MESSAGES",
    }
)


class ApiProtocolError(ValueError):
    """Raised when an API frame is malformed or exceeds a safety bound."""


@dataclass(frozen=True, slots=True)
class ApiMessage:
    type: str
    value: str
    params: Mapping[str, Any]

    @property
    def request_id(self) -> str | int | None:
        request_id = self.params.get("_ID")
        return request_id if isinstance(request_id, (str, int)) else None


def _bounded_json_object(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        raise ApiProtocolError("JSON nesting limit exceeded")
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
            raise ApiProtocolError("JSON string is too large")
        return value
    if isinstance(value, list):
        if len(value) > MAX_PARAMS:
            raise ApiProtocolError("JSON array is too large")
        return [_bounded_json_object(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > MAX_PARAMS:
            raise ApiProtocolError("JSON object has too many fields")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise ApiProtocolError("Invalid JSON object key")
            result[key] = _bounded_json_object(item, depth=depth + 1)
        return result
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ApiProtocolError("Unsupported JSON value")


def decode_line(line: bytes) -> ApiMessage:
    if len(line) > MAX_LINE_BYTES:
        raise ApiProtocolError("API line is too large")
    try:
        decoded = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiProtocolError("Invalid JSON API frame") from exc
    bounded = _bounded_json_object(decoded)
    if not isinstance(bounded, dict):
        raise ApiProtocolError("API frame must be a JSON object")
    event_type = bounded.get("type")
    value = bounded.get("value", "")
    params = bounded.get("params", {})
    if not isinstance(event_type, str) or not event_type or len(event_type) > 128:
        raise ApiProtocolError("API frame has invalid type")
    if not isinstance(value, str):
        raise ApiProtocolError("API frame value must be a string")
    if not isinstance(params, dict):
        raise ApiProtocolError("API frame params must be an object")
    return ApiMessage(event_type, value, params)


def encode_read_only_request(request_type: str, *, request_id: str) -> bytes:
    if request_type not in READ_ONLY_REQUESTS:
        raise ApiProtocolError(f"Request is not read-only: {request_type}")
    packet = {"params": {"_ID": request_id}, "type": request_type, "value": ""}
    encoded = (json.dumps(packet, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
    if len(encoded) > MAX_LINE_BYTES:
        raise ApiProtocolError("Encoded API request is too large")
    return encoded
