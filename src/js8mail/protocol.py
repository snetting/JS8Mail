"""JS8Mail enhanced envelope v1 delivery primitives.

The grammar is defined in ``docs/PROTOCOL_V1.md``. The important behavior is
selective acknowledgement, durable deduplication, bounded reassembly, and
honest delivery evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote

MAX_PARTS = 255
MAX_PART_BYTES = 4096
MAX_FRAME_BYTES = 4096
DISPLAY_VERSION = "JS8Mail/0.0.7"
CAPABILITY_PROTOCOL_VERSION = 1
CAPABILITY_TTL_MS = 7 * 24 * 60 * 60 * 1000
CAPABILITY_FEATURES = frozenset({"E2E", "MP", "PA", "RR"})
JS8MAIL_MARKER_RE = re.compile(r"\[JS8MAIL/\d+\.\d+\.\d+\]", re.IGNORECASE)
JS8MAIL_MARKER_PREFIX_RE = re.compile(r"^\s*\[JS8MAIL/\d+\.\d+\.\d+\]\s*", re.IGNORECASE)
JS8MAIL_MARKER_SUFFIX_RE = re.compile(r"\s*\[JS8MAIL/\d+\.\d+\.\d+\]\s*$", re.IGNORECASE)
CAPABILITY_FRAME_RE = re.compile(
    # JS8Call activity fragments can concatenate at a frame boundary, so the
    # separator between the numeric protocol version and the first feature
    # may be absent (``CAP 1E2E,MP``). Capture the fields separately and
    # canonicalise them before passing them to the strict parser.
    r"J8M1\s+CAP\s+(?P<version>\d+)\s*(?P<features>[A-Z][A-Z0-9]*(?:\s*,\s*[A-Z][A-Z0-9]*)*)",
    re.IGNORECASE,
)
_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_STATION_RE = re.compile(r"^[A-Z0-9/]{1,16}$")
_ADDRESS_RE = re.compile(r"^@?[A-Z0-9/]{1,16}$")
_SUBJECT_PREFIX_RE = re.compile(r"^\{S:([A-Za-z0-9._~%+-]{1,240})\}\|\s*", re.IGNORECASE)
_CONTROL_FRAME_RE = re.compile(r"\bJ8M1\s+(?:CAP|PA|REQ|DELIVERED)\b.*$", re.IGNORECASE)


def canonical_message_id(message_id: str) -> str:
    """JS8Call uppercases on-air text; IDs are case-insensitive on the wire."""
    if _MESSAGE_ID_RE.fullmatch(message_id) is None:
        raise MultipartError("invalid message id")
    return message_id.lower()


class MultipartError(ValueError):
    """Raised for invalid or inconsistent multipart data."""


def format_capability(capabilities: tuple[str, ...] = ("E2E", "MP", "PA")) -> str:
    """Format the small, versioned capability advertisement."""
    normalized = tuple(dict.fromkeys(cap.upper() for cap in capabilities))
    if not normalized or any(cap not in CAPABILITY_FEATURES for cap in normalized):
        raise MultipartError("invalid JS8Mail capability")
    result = f"J8M1 CAP {CAPABILITY_PROTOCOL_VERSION} {','.join(normalized)}"
    if len(result.encode()) > MAX_FRAME_BYTES:
        raise MultipartError("capability advertisement is too large")
    return result


def parse_capability(text: str) -> tuple[int, tuple[str, ...]] | None:
    """Parse a bounded capability advertisement from a received frame."""
    fields = text.strip().split()
    if len(fields) != 4 or [field.upper() for field in fields[:2]] != ["J8M1", "CAP"]:
        return None
    try:
        version = int(fields[2])
    except ValueError:
        return None
    capabilities = tuple(dict.fromkeys(cap.upper() for cap in fields[3].split(",")))
    if (
        version != CAPABILITY_PROTOCOL_VERSION
        or not capabilities
        or len(capabilities) > 8
        or any(cap not in CAPABILITY_FEATURES for cap in capabilities)
    ):
        return None
    return version, capabilities


def find_capability(text: str) -> tuple[int, tuple[str, ...]] | None:
    """Find a CAP frame embedded in an overheard JS8Call activity line."""
    match = CAPABILITY_FRAME_RE.search(text)
    if match is None:
        return None
    features = re.sub(r"\s+", "", match.group("features"))
    normalized = f"J8M1 CAP {match.group('version')} {features}"
    return parse_capability(normalized)


def contains_js8mail_marker(text: str) -> bool:
    """Return whether readable text carries a versioned JS8Mail marker.

    The marker is an interoperability hint only; it is never treated as
    proof of enhanced capability until a valid ``J8M1 CAP`` is received.
    """
    return JS8MAIL_MARKER_RE.search(text) is not None


def clean_user_message(text: str) -> str:
    """Remove an optional leading or trailing JS8Mail marker from mailbox text."""

    cleaned = JS8MAIL_MARKER_PREFIX_RE.sub("", text, count=1)
    return JS8MAIL_MARKER_SUFFIX_RE.sub("", cleaned, count=1).strip()


def is_js8mail_wire_frame(text: str) -> bool:
    """Return whether text is protocol traffic rather than user mail."""

    normalized = text.strip()
    if re.match(r"^MSG(?=J8M1\s)", normalized, re.IGNORECASE):
        normalized = normalized[3:].lstrip()
    return bool(re.match(r"^J8M1\s+(?:CAP|D|PA|REQ|DELIVERED)\b", normalized, re.IGNORECASE))


def extract_js8mail_control(text: str) -> str | None:
    """Extract a complete-looking JS8M control payload from display text.

    JS8Call activity can retain the addressed destination (or a display
    prefix) immediately before the protocol marker.  Data parts have their
    own strict parser; control receipts need this small normalization before
    ``parse_ack`` can be applied.  Completeness is still established by the
    activity reassembler, and the individual parsers remain strict.
    """

    match = _CONTROL_FRAME_RE.search(text.strip())
    return match.group(0).strip() if match is not None else None


def extract_envelope_subject(payload: str) -> tuple[str, str]:
    """Return the optional first-part subject and user payload."""

    match = _SUBJECT_PREFIX_RE.match(payload.strip())
    if match is None:
        return "", payload
    try:
        subject = unquote(match.group(1))
    except ValueError:
        return "", payload
    return (subject[:120], payload[match.end() :]) if subject else ("", payload[match.end() :])


@dataclass(frozen=True, slots=True)
class MessagePart:
    message_id: str
    number: int
    total: int
    payload: str
    origin: str = ""
    destination: str = ""

    def __post_init__(self) -> None:
        if _MESSAGE_ID_RE.fullmatch(self.message_id) is None:
            raise MultipartError("invalid message id")
        if not 1 <= self.total <= MAX_PARTS or not 1 <= self.number <= self.total:
            raise MultipartError("invalid multipart position")
        if len(self.payload.encode()) > MAX_PART_BYTES:
            raise MultipartError("multipart payload is too large")
        for value in (self.origin, self.destination):
            if value and _ADDRESS_RE.fullmatch(value.upper()) is None:
                raise MultipartError("invalid multipart route context")


@dataclass(frozen=True, slots=True)
class PartReceipt:
    message_id: str
    total: int
    received: tuple[int, ...]
    missing: tuple[int, ...]

    @property
    def complete(self) -> bool:
        return not self.missing

    def as_bitmap(self) -> str:
        """Return a compact hexadecimal bitmap, part 1 represented by bit 0."""
        bitmap = 0
        for number in self.received:
            bitmap |= 1 << (number - 1)
        width = (self.total + 3) // 4
        return f"{bitmap:0{width}X}"


class MultipartAccumulator:
    """Bounded, duplicate-safe reassembly for one enhanced message."""

    def __init__(self, message_id: str, total: int) -> None:
        if not message_id or not 1 <= total <= MAX_PARTS:
            raise MultipartError("invalid reassembly envelope")
        self.message_id = message_id
        self.total = total
        self._parts: dict[int, str] = {}
        self._last_receipt_ms: int | None = None
        self._last_receipt_received: tuple[int, ...] = ()

    def add(self, part: MessagePart) -> bool:
        if part.message_id != self.message_id or part.total != self.total:
            raise MultipartError("part does not match reassembly envelope")
        if part.number in self._parts:
            return False
        self._parts[part.number] = part.payload
        return True

    def receipt(self) -> PartReceipt:
        received = tuple(sorted(self._parts))
        missing = tuple(number for number in range(1, self.total + 1) if number not in self._parts)
        return PartReceipt(self.message_id, self.total, received, missing)

    def assembled(self) -> str | None:
        receipt = self.receipt()
        if not receipt.complete:
            return None
        return "".join(self._parts[number] for number in range(1, self.total + 1))

    def partial_preview(self) -> str:
        """Return an emergency-use preview with explicit missing-part markers."""
        chunks: list[str] = []
        for number in range(1, self.total + 1):
            if number in self._parts:
                chunks.append(self._parts[number])
            else:
                chunks.append(f"[MISSING PART {number}/{self.total}]")
        return "".join(chunks)

    def should_ack(self, now_ms: int, *, min_interval_ms: int = 30_000) -> bool:
        """Rate-limit ACKs while still ACKing newly discovered parts."""
        received = tuple(sorted(self._parts))
        if received == self._last_receipt_received:
            return (
                self._last_receipt_ms is None or now_ms - self._last_receipt_ms >= min_interval_ms
            )
        self._last_receipt_received = received
        self._last_receipt_ms = now_ms
        return True


def format_part_ack(receipt: PartReceipt) -> str:
    """Format the v1 selective part acknowledgement."""
    return f"J8M1 PA {receipt.message_id} {receipt.total} {receipt.as_bitmap()}"


def parse_part_ack(text: str) -> PartReceipt | None:
    """Decode a selective ACK and derive exactly which parts are missing."""
    fields = text.strip().split()
    if len(fields) != 5 or fields[:2] != ["J8M1", "PA"]:
        return None
    message_id, total_text, bitmap_text = fields[2:]
    try:
        total = int(total_text)
        bitmap = int(bitmap_text, 16)
    except ValueError:
        return None
    if _MESSAGE_ID_RE.fullmatch(message_id) is None or not 1 <= total <= MAX_PARTS or bitmap < 0:
        return None
    if bitmap >> total:
        return None
    received = tuple(number for number in range(1, total + 1) if bitmap & (1 << (number - 1)))
    missing = tuple(number for number in range(1, total + 1) if number not in received)
    return PartReceipt(canonical_message_id(message_id), total, received, missing)


def format_resend_request(message_id: str, total: int, missing: tuple[int, ...]) -> str:
    """Request only missing parts, preserving emergency partial delivery."""
    if _MESSAGE_ID_RE.fullmatch(message_id) is None or not 1 <= total <= MAX_PARTS:
        raise MultipartError("invalid resend request")
    if any(not 1 <= number <= total for number in missing):
        raise MultipartError("invalid missing part")
    bitmap = sum(1 << (number - 1) for number in missing)
    result = f"J8M1 REQ {message_id} {total} {bitmap:X}"
    if len(result.encode()) > MAX_FRAME_BYTES:
        raise MultipartError("resend request is too large")
    return result


def parse_resend_request(text: str) -> tuple[str, int, tuple[int, ...]] | None:
    """Parse a bounded request for only the missing multipart sections."""
    fields = text.strip().split()
    if len(fields) != 5 or fields[:2] != ["J8M1", "REQ"]:
        return None
    message_id, total_text, bitmap_text = fields[2:]
    try:
        total = int(total_text)
        bitmap = int(bitmap_text, 16)
    except ValueError:
        return None
    if _MESSAGE_ID_RE.fullmatch(message_id) is None or not 1 <= total <= MAX_PARTS or bitmap < 0:
        return None
    if bitmap >> total:
        return None
    missing = tuple(number for number in range(1, total + 1) if bitmap & (1 << (number - 1)))
    return canonical_message_id(message_id), total, missing


def format_delivery_ack(message_id: str, delivered_at_ms: int, path: tuple[str, ...] = ()) -> str:
    """Format an end-to-end delivery receipt for the original sender."""
    if _MESSAGE_ID_RE.fullmatch(message_id) is None:
        raise MultipartError("invalid message id")
    if delivered_at_ms < 0 or any(
        _STATION_RE.fullmatch(station.upper()) is None for station in path
    ):
        raise MultipartError("invalid delivery metadata")
    path_text = ",".join(path[:8]) or "?"
    result = f"J8M1 DELIVERED {message_id} {delivered_at_ms} {path_text}"
    if len(result.encode()) > MAX_FRAME_BYTES:
        raise MultipartError("delivery receipt is too large")
    return result


def parse_delivery_ack(text: str) -> tuple[str, int, tuple[str, ...]] | None:
    """Parse delivery time and the bounded path carried by a receipt."""
    fields = text.strip().split()
    if len(fields) != 5 or fields[:2] != ["J8M1", "DELIVERED"]:
        return None
    message_id, timestamp, path_text = fields[2:]
    if _MESSAGE_ID_RE.fullmatch(message_id) is None or not path_text:
        return None
    try:
        delivered_at_ms = int(timestamp)
    except ValueError:
        return None
    path = tuple(path_text.split(","))
    if (
        delivered_at_ms < 0
        or len(path) > 8
        or (
            path != ("?",)
            and any(_STATION_RE.fullmatch(station.upper()) is None for station in path)
        )
    ):
        return None
    return canonical_message_id(message_id), delivered_at_ms, path


def parse_ack(text: str) -> tuple[str, str, str | None] | None:
    """Parse bounded v1 acknowledgement syntax.

    Returns ``(kind, message_id, bitmap)`` for ``PA`` and ``DELIVERED``
    frames. Unknown or malformed frames are ignored by design.
    """
    if len(text.encode()) > MAX_FRAME_BYTES:
        return None
    part = parse_part_ack(text)
    if part is not None:
        return ("part", part.message_id, part.as_bitmap())
    delivery = parse_delivery_ack(text)
    if delivery is not None:
        return ("delivered", delivery[0], None)
    return None


def format_human_data_part(
    part: MessagePart, origin: str = "", destination: str = "", subject: str = ""
) -> str:
    """Format a readable body part with a compact JS8Mail correlation prefix.

    The body is intentionally not compressed or binary-packed here. JS8Call
    receives readable text and remains responsible for its own token/varicode
    encoding. The prefix is defined by the v1 protocol specification.
    """
    origin = origin or part.origin
    destination = destination or part.destination
    payload = part.payload
    if subject and part.number == 1:
        encoded_subject = quote(subject[:120], safe="._~-")
        payload = f"{{S:{encoded_subject}}}| {payload}"
    if origin or destination:
        if (
            not origin
            or not destination
            or any(_ADDRESS_RE.fullmatch(value.upper()) is None for value in (origin, destination))
        ):
            raise MultipartError("invalid multipart route context")
        result = (
            f"J8M1 D {origin} {destination} {part.message_id} {part.number}/{part.total} {payload}"
        )
    else:
        result = f"J8M1 D {part.message_id} {part.number}/{part.total} {payload}"
    if len(result.encode()) > MAX_FRAME_BYTES:
        raise MultipartError("multipart frame is too large")
    return result


def parse_human_data_part(
    text: str,
) -> tuple[MessagePart, str | None, str | None] | None:
    """Parse old compact or origin-aware v1 multipart data."""
    fields = text.strip().split()
    if len(fields) < 5 or [field.upper() for field in fields[:2]] != ["J8M1", "D"]:
        return None
    origin: str | None = None
    destination: str | None = None
    if len(fields) >= 7 and "/" in fields[5]:
        origin, destination, message_id, position = fields[2:6]
        payload = " ".join(fields[6:])
    else:
        message_id, position = fields[2:4]
        payload = " ".join(fields[4:])
    try:
        number_text, total_text = position.split("/", 1)
        part = MessagePart(
            canonical_message_id(message_id),
            int(number_text),
            int(total_text),
            payload,
            origin or "",
            destination or "",
        )
    except (ValueError, MultipartError):
        return None
    return part, origin, destination


def split_human_message(
    message_id: str, body: str, chunk_bytes: int = 180
) -> tuple[MessagePart, ...]:
    """Split readable data into bounded JS8Mail parts for enhanced peers."""
    if chunk_bytes < 32 or chunk_bytes > MAX_PART_BYTES:
        raise MultipartError("invalid part size")
    chunks_list: list[str] = []
    current = ""
    current_bytes = 0
    for character in body:
        character_bytes = len(character.encode())
        if current and current_bytes + character_bytes > chunk_bytes:
            chunks_list.append(current)
            current = ""
            current_bytes = 0
        current += character
        current_bytes += character_bytes
    if current:
        chunks_list.append(current)
    chunks = tuple(chunks_list)
    total = len(chunks)
    if not total:
        raise MultipartError("message body is empty")
    return tuple(
        MessagePart(message_id, number, total, payload) for number, payload in enumerate(chunks, 1)
    )


def format_ordinary_message(destination: str, body: str, announce: bool = False) -> str:
    """Build a standard JS8Call message with an optional trailing identifier."""
    suffix = f" [{DISPLAY_VERSION}]" if announce else ""
    result = f"{destination} MSG {body}{suffix}"
    if len(result.encode()) > MAX_FRAME_BYTES:
        raise MultipartError("ordinary message is too large")
    return result


def format_standard_user_payload(subject: str, body: str) -> str:
    """Render local subject metadata as readable Standard JS8Call text."""

    subject = subject.strip()[:120]
    return f"{subject}: {body}" if subject else body


def format_relay_message(path: tuple[str, ...], body: str) -> str:
    """Build JS8Call's standard relay form for a discovered path."""
    if len(path) < 3 or any(_STATION_RE.fullmatch(call.upper()) is None for call in path):
        raise MultipartError("a relay path needs at least three callsigns")
    if any(" " in call or ">" in call for call in path):
        raise MultipartError("invalid relay callsign")
    # JS8Call's relay grammar separates the final directed command from the
    # relay path with another '>'.  Omitting it makes the command ambiguous
    # to the receiving JS8Call instance and prevents the normal ACK path.
    result = f"{path[1]}>{'>'.join(path[2:])}>MSG {body}"
    if len(result.encode()) > MAX_FRAME_BYTES:
        raise MultipartError("relay message is too large")
    return result


def format_relay_text(path: tuple[str, ...], body: str) -> str:
    """Build a JS8Call free-text relay, without the MSG command."""
    if len(path) < 3 or any(_STATION_RE.fullmatch(call.upper()) is None for call in path):
        raise MultipartError("a relay path needs at least three callsigns")
    if any(" " in call or ">" in call for call in path):
        raise MultipartError("invalid relay callsign")
    result = f"{path[1]}>{'>'.join(path[2:])}>{body}"
    if len(result.encode()) > MAX_FRAME_BYTES:
        raise MultipartError("relay text is too large")
    return result


def format_store_message(custodian: str, destination: str, body: str) -> str:
    """Build JS8Call's standard store-and-forward request."""
    if not custodian or not destination or len(custodian) > 16 or len(destination) > 16:
        raise MultipartError("invalid store-and-forward callsign")
    if any(char in custodian + destination for char in " >"):
        raise MultipartError("invalid store-and-forward callsign")
    result = f"{custodian} MSG TO:{destination} {body}"
    if len(result.encode()) > MAX_FRAME_BYTES:
        raise MultipartError("stored message is too large")
    return result
