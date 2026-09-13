"""Rate-limited standard JS8Call discovery and retrieval policy."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True)
class QueryState:
    attempts: int = 0
    last_at_ms: int | None = None
    next_at_ms: int = 0


class QueryScheduler:
    def __init__(self, *, base_delay_ms: int = 60_000, max_delay_ms: int = 1_800_000) -> None:
        self.base_delay_ms = base_delay_ms
        self.max_delay_ms = max_delay_ms
        self._states: dict[str, QueryState] = {}

    def due(self, key: str, now_ms: int) -> bool:
        return now_ms >= self._states.get(key, QueryState()).next_at_ms

    def record(self, key: str, now_ms: int, *, success: bool = False) -> None:
        state = self._states.setdefault(key, QueryState())
        state.attempts = 0 if success else state.attempts + 1
        state.last_at_ms = now_ms
        delay = (
            self.base_delay_ms
            if success
            else min(self.base_delay_ms * (2 ** min(state.attempts - 1, 5)), self.max_delay_ms)
        )
        state.next_at_ms = now_ms + delay

    def state(self, key: str) -> QueryState:
        return self._states.get(key, QueryState())

    def restore(self, key: str, now_ms: int, remaining_ms: int) -> None:
        """Restore a cooldown reconstructed from durable wall-clock history."""
        self._states[key] = QueryState(last_at_ms=now_ms, next_at_ms=now_ms + max(0, remaining_ms))


def hearing_query(callsign: str) -> str:
    return f"{callsign.strip().upper()} HEARING?"


def snr_query(callsign: str) -> str:
    return f"{callsign.strip().upper()} SNR?"


def parse_query_call_response(text: str) -> tuple[int, int] | None:
    """Parse JS8Call's ``CALL YES -08 (1M)`` response.

    Returns ``(snr_db, age_minutes)``. The response is deliberately kept
    small and tolerant because JS8Call may omit the age field.
    """
    fields = text.strip().split()
    if len(fields) < 3 or fields[1].upper() != "YES":
        return None
    try:
        snr = int(fields[2])
    except ValueError:
        return None
    age = 0
    if len(fields) >= 4:
        match = re.fullmatch(r"\((\d+)([MH])\)", fields[3].upper())
        if match:
            age = int(match.group(1)) * (60 if match.group(2) == "H" else 1)
    if not -60 <= snr <= 60 or age > 24 * 60:
        return None
    return snr, age


def call_query(callsign: str) -> str:
    return f"@ALLCALL QUERY CALL {callsign.strip().upper()}"


def messages_query() -> str:
    return "@ALLCALL QUERY MSGS"


def custodian_messages_query(custodian: str) -> str:
    return f"{custodian.strip().upper()} QUERY MSGS"


def retrieve_message_query(custodian: str, message_id: int) -> str:
    if not 0 <= message_id <= 2_147_483_647:
        raise ValueError("invalid JS8Call message id")
    return f"{custodian.strip().upper()} QUERY MSG {message_id}"


def parse_messages_available(text: str) -> int | None:
    """Parse JS8Call's ``YES MSG ID N`` custodian response."""
    fields = text.strip().split()
    if len(fields) != 4 or fields[:3] != ["YES", "MSG", "ID"]:
        return None
    try:
        value = int(fields[3])
    except ValueError:
        return None
    return value if 0 <= value <= 2_147_483_647 else None
