"""Conservative reassembly of JS8Call ``RX.ACTIVITY`` message frames.

``RX.ACTIVITY`` is a display/activity stream, not the authoritative directed
message API.  Some JS8Call builds expose a long directed message there as a
sequence of short frames.  The stream has no application sequence number, so
this module deliberately refuses to guess when two streams could match the
same continuation.  The documented ``BITS`` first/last flags are used when
present; the caller may use the returned partial text for emergency display.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

_START_RE = re.compile(
    r"^\s*([A-Z0-9/]{1,16})\s*:\s*([A-Z0-9/]{1,16})\s+MSG(?:\s+(.*?))?\s*$",
    re.IGNORECASE,
)
_DIRECTED_START_RE = re.compile(
    r"^\s*([A-Z0-9/]{1,16})\s*:\s*([A-Z0-9/]{1,16})(?:\s+(.*?))?\s*$",
    re.IGNORECASE,
)
_CALLSIGN_PREFIX_RE = re.compile(r"^\s*[A-Z0-9/]{1,16}\s*:", re.IGNORECASE)
_CONTROL_START_RE = re.compile(r"^\s*J8M1\s+(?:CAP|PA|REQ|DELIVERED)\b", re.IGNORECASE)
_ELLIPSIS_RE = re.compile(r"(?:…{2,}|\.{3,})\s*[♢◊]?\s*$")


def first_frame(bits: int | None) -> bool:
    """Return whether JS8Call's documented first-frame bit is set."""

    return bits is not None and bool(bits & 1)


def last_frame(bits: int | None) -> bool:
    """Return whether JS8Call's documented last-frame bit is set."""

    return bits is not None and bool(bits & 2)


@dataclass(frozen=True, slots=True)
class ActivityFragment:
    value: str
    bits: int | None
    observed_at_ms: int
    received_at_ms: int
    band: str = ""
    dial_frequency: int | None = None
    offset: int | None = None
    speed: int | None = None


@dataclass(frozen=True, slots=True)
class ActivityAssembly:
    stream_id: str
    source: str
    destination: str
    text: str
    complete: bool
    confidence: str
    ambiguous: bool = False
    gap_suspected: bool = False


@dataclass(slots=True)
class _Stream:
    stream_id: str
    source: str
    destination: str
    parts: list[str] = field(default_factory=list)
    fragments: int = 0
    first_observed_at_ms: int = 0
    last_observed_at_ms: int = 0
    last_received_at_ms: int = 0
    band: str = ""
    dial_frequency: int | None = None
    offset: int | None = None
    speed: int | None = None
    ambiguous: bool = False
    gap_suspected: bool = False


class ActivityAssembler:
    """Assemble locally addressed activity frames without cross-stream guesses.

    A continuation is accepted only when exactly one live stream has a
    compatible RF context.  A callsign-prefixed activity line without the
    first bit is treated as another observed message and is never appended to
    a pending stream.  This is the important distinction that lets unrelated
    heartbeat/QSO traffic interleave safely.
    """

    def __init__(
        self,
        *,
        max_age_ms: int = 15 * 60 * 1000,
        max_gap_ms: int = 8 * 60 * 1000,
        accept_control_starts: bool = False,
    ) -> None:
        self.max_age_ms = max_age_ms
        self.max_gap_ms = max_gap_ms
        # CAP/control streams may not begin with MSG. This is opt-in so the
        # ordinary message assembler remains conservative.
        self.accept_control_starts = accept_control_starts
        self._streams: dict[str, _Stream] = {}

    @property
    def active_streams(self) -> tuple[ActivityAssembly, ...]:
        return tuple(self._snapshot(stream) for stream in self._streams.values())

    def _snapshot(self, stream: _Stream) -> ActivityAssembly:
        text = "".join(stream.parts).strip()
        return ActivityAssembly(
            stream.stream_id,
            stream.source,
            stream.destination,
            text,
            False,
            "ambiguous" if stream.ambiguous else "partial",
            stream.ambiguous,
            stream.gap_suspected,
        )

    @staticmethod
    def _context_matches(stream: _Stream, fragment: ActivityFragment) -> bool:
        for left, right in (
            (stream.band, fragment.band),
            (stream.dial_frequency, fragment.dial_frequency),
            (stream.speed, fragment.speed),
        ):
            if left not in (None, "", 0) and right not in (None, "", 0) and left != right:
                return False
        return not (
            stream.offset is not None
            and fragment.offset is not None
            and abs(stream.offset - fragment.offset) > 12
        )

    def _expire(self, now_received_at_ms: int) -> None:
        expired = [
            stream_id
            for stream_id, stream in self._streams.items()
            if now_received_at_ms - stream.last_received_at_ms > self.max_age_ms
        ]
        for stream_id in expired:
            del self._streams[stream_id]

    def feed(
        self,
        fragment: ActivityFragment,
        *,
        local_destination: str | None,
    ) -> tuple[ActivityAssembly, ...]:
        """Consume one activity frame and return completed/partial updates."""

        self._expire(fragment.received_at_ms)
        text = fragment.value.strip()
        start = _START_RE.match(text)
        bare_control = False
        if start is None and self.accept_control_starts:
            start = _DIRECTED_START_RE.match(text)
            if start is None and not self._streams and _CONTROL_START_RE.match(text) is not None:
                # Some JS8Call builds expose a directed control train without
                # the first-frame address prefix.  The protocol marker itself
                # is still an unambiguous stream boundary; the application
                # infers/validates the peer from the completed control frame.
                bare_control = True
        is_first = first_frame(fragment.bits)
        is_last = last_frame(fragment.bits)
        if (start is not None or bare_control) and (
            is_first or fragment.bits is None or bare_control
        ):
            if bare_control:
                source, destination = "", ""
                seed_text = text
                is_message = False
            elif start is not None:
                source, destination = start.group(1).upper(), start.group(2).upper()
                seed = start.group(3) or ""
                is_message = _START_RE.match(text) is not None
                seed_text = (
                    f"{source}: {destination}"
                    + (" MSG" if is_message else "")
                    + (f" {seed}" if seed else "")
                )
            else:
                return ()
            # ``None`` is used by the control-plane observer for overheard
            # CAP exchanges.  It may learn both endpoints, but the caller
            # still decides whether a response is permitted.
            if local_destination is not None and destination != local_destination.strip().upper():
                return ()
            digest = hashlib.sha256(
                f"{source}\n{destination}\n{fragment.received_at_ms}\n{seed_text}".encode()
            ).hexdigest()[:20]
            stream = _Stream(
                f"activity-{digest}",
                source,
                destination,
                [seed_text],
                1,
                fragment.observed_at_ms,
                fragment.observed_at_ms,
                fragment.received_at_ms,
                fragment.band,
                fragment.dial_frequency,
                fragment.offset,
                fragment.speed,
            )
            self._streams[stream.stream_id] = stream
            if is_last:
                del self._streams[stream.stream_id]
                return (
                    ActivityAssembly(
                        stream.stream_id,
                        source,
                        destination,
                        "".join(stream.parts),
                        True,
                        "authoritative" if fragment.bits is not None else "legacy",
                    ),
                )
            return (self._snapshot(stream),)

        # A first-frame activity line is a new stream only if it is a locally
        # addressed MSG.  Other starts must not destroy a pending stream.
        if is_first:
            return ()
        if not self._streams or _CALLSIGN_PREFIX_RE.match(text):
            return ()
        candidates = [
            stream
            for stream in self._streams.values()
            if self._context_matches(stream, fragment)
            and fragment.received_at_ms - stream.last_received_at_ms <= self.max_age_ms
        ]
        if len(candidates) != 1:
            if len(candidates) > 1:
                for stream in candidates:
                    stream.ambiguous = True
            return tuple(self._snapshot(stream) for stream in candidates)
        stream = candidates[0]
        if fragment.observed_at_ms + 2000 < stream.last_observed_at_ms:
            stream.ambiguous = True
        if fragment.received_at_ms - stream.last_received_at_ms > self.max_gap_ms:
            stream.gap_suspected = True
        stream.parts.append(text)
        stream.fragments += 1
        stream.last_observed_at_ms = max(stream.last_observed_at_ms, fragment.observed_at_ms)
        stream.last_received_at_ms = fragment.received_at_ms
        legacy_last = fragment.bits is None and bool(_ELLIPSIS_RE.search(text))
        if is_last or legacy_last:
            result = ActivityAssembly(
                stream.stream_id,
                stream.source,
                stream.destination,
                "".join(stream.parts),
                not stream.ambiguous,
                ("reassembled" if fragment.bits is not None else "legacy")
                if not stream.ambiguous
                else "ambiguous",
                stream.ambiguous,
                stream.gap_suspected,
            )
            del self._streams[stream.stream_id]
            return (result,)
        return (self._snapshot(stream),)

    @staticmethod
    def clean_text(text: str) -> str:
        """Remove JS8Call continuation/EOT decoration from display text."""

        return _ELLIPSIS_RE.sub("", text).rstrip("♢◊ ").strip()
