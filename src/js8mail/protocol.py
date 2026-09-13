"""Provisional JS8Mail multipart delivery primitives.

The wire grammar is intentionally isolated here until the measured v1 envelope
is ratified. The important behavior is selective acknowledgement, durable
deduplication, bounded reassembly, and honest delivery evidence.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_PARTS = 255
MAX_PART_BYTES = 4096
MAX_FRAME_BYTES = 4096
DISPLAY_VERSION = "JS8Mail/0.0.1"


class MultipartError(ValueError):
    """Raised for invalid or inconsistent multipart data."""


@dataclass(frozen=True, slots=True)
class MessagePart:
    message_id: str
    number: int
    total: int
    payload: str

    def __post_init__(self) -> None:
        if not self.message_id or len(self.message_id) > 32:
            raise MultipartError("invalid message id")
        if not 1 <= self.total <= MAX_PARTS or not 1 <= self.number <= self.total:
            raise MultipartError("invalid multipart position")
        if len(self.payload.encode()) > MAX_PART_BYTES:
            raise MultipartError("multipart payload is too large")


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
    """Format the provisional selective ACK for later protocol ratification."""
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
    if not message_id or len(message_id) > 32 or not 1 <= total <= MAX_PARTS or bitmap < 0:
        return None
    if bitmap >> total:
        return None
    received = tuple(number for number in range(1, total + 1) if bitmap & (1 << (number - 1)))
    missing = tuple(number for number in range(1, total + 1) if number not in received)
    return PartReceipt(message_id, total, received, missing)


def format_resend_request(message_id: str, total: int, missing: tuple[int, ...]) -> str:
    """Request only missing parts, preserving emergency partial delivery."""
    if not message_id or len(message_id) > 32 or not 1 <= total <= MAX_PARTS:
        raise MultipartError("invalid resend request")
    if any(not 1 <= number <= total for number in missing):
        raise MultipartError("invalid missing part")
    bitmap = sum(1 << (number - 1) for number in missing)
    result = f"J8M1 REQ {message_id} {total} {bitmap:X}"
    if len(result.encode()) > MAX_FRAME_BYTES:
        raise MultipartError("resend request is too large")
    return result


def format_delivery_ack(
    message_id: str, delivered_at_ms: int, path: tuple[str, ...] = ()
) -> str:
    """Format an end-to-end delivery receipt for the original sender."""
    if not message_id or len(message_id) > 32:
        raise MultipartError("invalid message id")
    if delivered_at_ms < 0 or any(not station or len(station) > 16 for station in path):
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
    if not message_id or len(message_id) > 32 or not path_text:
        return None
    try:
        delivered_at_ms = int(timestamp)
    except ValueError:
        return None
    path = tuple(path_text.split(","))
    if delivered_at_ms < 0 or len(path) > 8 or any(not station or len(station) > 16 for station in path):
        return None
    return message_id, delivered_at_ms, path


def parse_ack(text: str) -> tuple[str, str, str | None] | None:
    """Parse bounded provisional ACK syntax.

    Returns ``(kind, message_id, bitmap)`` for ``PA`` and ``DELIVERED``
    frames. Unknown or malformed frames are ignored by design.
    """
    if len(text.encode()) > MAX_FRAME_BYTES:
        return None
    fields = text.strip().split()
    if len(fields) == 5 and fields[0] == "J8M1" and fields[1] == "PA":
        message_id, total, bitmap = fields[2:]
        if not message_id or len(message_id) > 32:
            return None
        try:
            if not 1 <= int(total) <= MAX_PARTS or not bitmap or int(bitmap, 16) < 0:
                return None
        except ValueError:
            return None
        return ("part", message_id, bitmap.upper())
    if len(fields) >= 3 and fields[0] == "J8M1" and fields[1] == "DELIVERED":
        message_id = fields[2]
        if message_id and len(message_id) <= 32:
            return ("delivered", message_id, None)
    return None


def format_human_data_part(part: MessagePart) -> str:
    """Format a readable body part with a compact JS8Mail correlation prefix.

    The body is intentionally not compressed or binary-packed here. JS8Call
    receives readable text and remains responsible for its own token/varicode
    encoding. The prefix is provisional until protocol v1 is ratified.
    """
    return f"J8M1 D {part.message_id} {part.number}/{part.total} {part.payload}"


def split_human_message(message_id: str, body: str, chunk_bytes: int = 180) -> tuple[MessagePart, ...]:
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
    return tuple(MessagePart(message_id, number, total, payload) for number, payload in enumerate(chunks, 1))


def format_ordinary_message(destination: str, body: str, announce: bool = False) -> str:
    """Build a standard JS8Call message with an optional visible identifier."""
    prefix = f"[{DISPLAY_VERSION}] " if announce else ""
    result = f"{destination} MSG {prefix}{body}"
    if len(result.encode()) > MAX_FRAME_BYTES:
        raise MultipartError("ordinary message is too large")
    return result
