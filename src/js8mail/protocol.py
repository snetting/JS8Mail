"""Provisional JS8Mail multipart delivery primitives.

The wire grammar is intentionally isolated here until the measured v1 envelope
is ratified. The important behavior is selective acknowledgement, durable
deduplication, bounded reassembly, and honest delivery evidence.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_PARTS = 255
MAX_PART_BYTES = 4096


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


def format_human_data_part(part: MessagePart) -> str:
    """Format a readable body part with a compact JS8Mail correlation prefix.

    The body is intentionally not compressed or binary-packed here. JS8Call
    receives readable text and remains responsible for its own token/varicode
    encoding. The prefix is provisional until protocol v1 is ratified.
    """
    return f"J8M1 D {part.message_id} {part.number}/{part.total} {part.payload}"
