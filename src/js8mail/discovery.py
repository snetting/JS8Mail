"""Rate-limited standard JS8Call discovery and retrieval policy."""

from __future__ import annotations

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


def hearing_query(callsign: str) -> str:
    return f"{callsign.strip().upper()} HEARING?"


def snr_query(callsign: str) -> str:
    return f"{callsign.strip().upper()} SNR?"


def call_query(callsign: str) -> str:
    return f"@ALLCALL QUERY CALL {callsign.strip().upper()}"


def messages_query() -> str:
    return "@ALLCALL QUERY MSGS"
