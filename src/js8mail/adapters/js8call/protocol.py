"""Bounded JSON-line protocol handling for JS8Call's local API.

Transmit requests are kept explicit and separately validated from read-only
requests. Higher layers still decide whether an operator approved sending.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from js8mail.domain import NormalizedEvent

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

TRANSMIT_REQUESTS = frozenset({"TX.SET_TEXT", "TX.SEND_MESSAGE"})
# The halt command is deliberately isolated from ordinary transmit requests:
# it can only stop an active JS8Call transmission and is used by the local
# operator pause/kill control.
CONTROL_REQUESTS = frozenset({"RIG.TX_HALT"})
# These are the values used by JS8Call's API, not a sequential enum.
# JS8-60 (8) is experimental but is still a valid API value.
SPEED_VALUES = frozenset({0, 1, 2, 4, 8})


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


@dataclass(frozen=True, slots=True)
class DirectedFrame:
    """Semantic view of a JS8Call directed event.

    JS8Call versions have emitted both a display-oriented ``value`` and a
    command-oriented ``params.TEXT``.  The latter commonly contains the
    destination and command again, so application code must not parse either
    field as an application payload without normalizing it first.
    """

    source: str
    destination: str
    command: str
    payload: str
    wire_text: str
    stored_recipient: str = ""
    final: bool = True


def parse_legacy_ack(frame: DirectedFrame) -> tuple[str, tuple[str, ...]] | None:
    """Return the station that accepted a legacy JS8Call message.

    A direct ACK is exposed as ``CMD=ACK``.  A relayed final ACK is exposed by
    JS8Call as a ``CMD=>`` frame whose text contains ``ACK *DE* DEST``; the
    API's ``FROM`` field is then the relay that carried the response, not the
    station that accepted the message.  Only the explicit ``*DE*`` destination
    is trusted for a relayed ACK, so an intermediate hop cannot accidentally
    complete an origin-side message.
    """
    if frame.command == "ACK":
        return frame.source.upper(), (frame.source.upper(),)
    if frame.command != ">":
        return None
    text = f"{frame.wire_text} {frame.payload}".upper()
    match = re.search(r"\bACK\s+\*DE\*\s*([A-Z0-9/]{1,16})\b", text)
    if match is None:
        return None
    destination = match.group(1)
    path_text = text.split("ACK", 1)[0]
    path = tuple(
        token.strip()
        for token in path_text.split(">")
        if re.fullmatch(r"[A-Z0-9/]{1,16}", token.strip())
    )
    return destination, path + (destination,)


_EOT_RE = re.compile(r"\s*[♢◊]\s*$")
def _clean_directed_text(value: str) -> str:
    value = value.strip()
    value = _EOT_RE.sub("", value).strip()
    # Some versions include the source prefix in value/TEXT even though FROM
    # is also supplied as a structured parameter.
    value = re.sub(
        r"^\s*[@A-Z0-9/]{1,32}:\s+", "", value, count=1, flags=re.IGNORECASE
    )
    return value.strip()


def normalize_directed_event(event: NormalizedEvent) -> DirectedFrame | None:
    """Normalize an RX.DIRECTED frame from known JS8Call API variants."""
    params = event.params
    source = params.get("FROM")
    destination = params.get("TO")
    command_value = params.get("CMD")
    if not isinstance(source, str) or not isinstance(destination, str):
        return None
    if not isinstance(command_value, str):
        return None
    source = source.strip().upper()
    destination = destination.strip().upper()
    command = " ".join(command_value.strip().upper().split())
    text_value = params.get("TEXT")
    raw = text_value if isinstance(text_value, str) and text_value.strip() else event.value
    wire_text = _clean_directed_text(raw)

    # Remove the addressed destination from the command-oriented TEXT.  If a
    # build supplies only the command/payload, this is harmless.
    without_destination = wire_text
    if destination:
        without_destination = re.sub(
            rf"^\s*{re.escape(destination)}(?=\s|$)\s*",
            "",
            without_destination,
            count=1,
            flags=re.IGNORECASE,
        ).strip()

    # The API exposes MSG TO: as one command, while the human-readable text
    # may contain either TO:CALL or TO: CALL.
    stored_recipient = ""
    payload = without_destination
    if command == "MSG TO:":
        match = re.match(
            r"^MSG\s+TO:\s*([^\s]+)(?:\s+(.*))?$", payload, re.IGNORECASE
        )
        if match:
            stored_recipient = match.group(1).strip().upper()
            payload = (match.group(2) or "").strip()
    elif command:
        payload = re.sub(
            rf"^{re.escape(command)}(?=\s|$)\s*",
            "",
            payload,
            count=1,
            flags=re.IGNORECASE,
        ).strip()

    final = not bool(event.params.get("PARTIAL", False))
    return DirectedFrame(
        source,
        destination,
        command,
        _EOT_RE.sub("", payload).strip(),
        wire_text,
        stored_recipient,
        final,
    )


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


def encode_transmit_request(request_type: str, value: str, *, request_id: str) -> bytes:
    if request_type not in TRANSMIT_REQUESTS:
        raise ApiProtocolError(f"Request is not a supported transmit request: {request_type}")
    if request_type in TRANSMIT_REQUESTS and (not value or len(value.encode("utf-8")) > 4096):
        raise ApiProtocolError("Transmit text must contain 1–4096 UTF-8 bytes")
    packet = {"params": {"_ID": request_id}, "type": request_type, "value": value}
    encoded = (json.dumps(packet, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
    if len(encoded) > MAX_LINE_BYTES:
        raise ApiProtocolError("Encoded API request is too large")
    return encoded


def encode_speed_request(speed: int, *, request_id: str) -> bytes:
    """Encode the documented optional JS8Call mode-speed control."""
    if speed not in SPEED_VALUES:
        raise ApiProtocolError("Unsupported JS8Call speed")
    packet = {"params": {"_ID": request_id, "SPEED": speed}, "type": "MODE.SET_SPEED", "value": ""}
    encoded = (json.dumps(packet, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
    if len(encoded) > MAX_LINE_BYTES:
        raise ApiProtocolError("Encoded speed request is too large")
    return encoded


def encode_control_request(request_type: str, *, request_id: str) -> bytes:
    """Encode a bounded operator control request.

    JS8Call builds differ in whether ``RIG.TX_HALT`` is implemented.  Keeping
    it behind this adapter lets the caller attempt it safely while the local
    pause flag remains authoritative even when the command is unavailable.
    """
    if request_type not in CONTROL_REQUESTS:
        raise ApiProtocolError(f"Unsupported control request: {request_type}")
    packet = {"params": {"_ID": request_id}, "type": request_type, "value": ""}
    encoded = (json.dumps(packet, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
    if len(encoded) > MAX_LINE_BYTES:
        raise ApiProtocolError("Encoded control request is too large")
    return encoded
