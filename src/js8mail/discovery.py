"""Rate-limited standard JS8Call discovery and retrieval policy."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True)
class QueryState:
    attempts: int = 0
    last_at_ms: int | None = None
    next_at_ms: int = 0


@dataclass(frozen=True, slots=True)
class QueryCallResponse:
    """A positive QUERY CALL reply addressed to the original requester."""

    recipient: str | None
    snr: int | None = None
    age_minutes: float | None = None


@dataclass(frozen=True, slots=True)
class PendingCallQuery:
    """Context JS8Call omits from the compact ``CALL YES`` response."""

    submitted_at_ms: int
    destination: str
    responder: str
    scheduler_key: str
    band: str = ""
    response_window_ms: int = 90_000


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


def parse_query_call_response(text: str) -> QueryCallResponse | None:
    """Parse a positive JS8Call QUERY CALL response.

    JS8Call addresses the answer to the station that made the query; the
    queried callsign is not repeated. Both a recipient-prefixed response and
    the bare response are seen on air. Ages may be seconds, minutes, or hours.
    """
    match = re.match(
        r"^\s*(?:(?P<recipient>[A-Z0-9/]{1,16})\s+)?YES"
        r"(?:\s+(?P<snr>[+-]?\d{1,2}))?"
        r"(?:\s+\((?P<age>\d+)(?P<unit>[SMH])(?:\)|\b))?",
        text,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    snr = int(match.group("snr")) if match.group("snr") is not None else None
    age = float(match.group("age")) if match.group("age") is not None else None
    unit = match.group("unit")
    if age is not None and unit is not None:
        if unit.upper() == "S":
            age /= 60
        elif unit.upper() == "H":
            age *= 60
    if (snr is not None and not -60 <= snr <= 60) or (age is not None and age > 24 * 60):
        return None
    recipient = match.group("recipient")
    return QueryCallResponse(recipient.upper() if recipient else None, snr, age)


def correlate_query_call_response(
    pending: list[PendingCallQuery],
    responder: str,
    *,
    now_ms: int,
    band: str = "",
    max_age_ms: int = 180_000,
    allow_late: bool = False,
) -> PendingCallQuery | None:
    """Find the one unambiguous query represented by a compact YES reply.

    A directed query to the responder is preferred over an @ALLCALL query.
    If more than one destination remains possible, no route is inferred.
    """
    responder = responder.strip().upper()
    band = band.strip().lower()
    active = [
        query
        for query in pending
        if 0
        <= now_ms - query.submitted_at_ms
        <= (max_age_ms if allow_late else min(max_age_ms, query.response_window_ms))
        and (not band or not query.band or query.band.lower() == band)
    ]
    exact = [query for query in active if query.responder == responder]
    candidates = exact or [query for query in active if query.responder == "@ALLCALL"]
    if len({query.destination for query in candidates}) != 1:
        return None
    return max(candidates, key=lambda query: query.submitted_at_ms, default=None)


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
    parsed = parse_messages_available_context(text)
    return parsed[2] if parsed is not None else None


def parse_messages_available_context(text: str) -> tuple[str | None, str | None, int] | None:
    """Parse a stored-message announcement and its optional display context.

    JS8Call API variants expose the responder/destination either as structured
    fields or in display text such as ``MM0ZFG: OH3SPN YES MSG ID 431``.  The
    numeric ID alone is not enough for retrieval because it is local to the
    custodian's JS8Call inbox.
    """
    cleaned = re.sub(r"\s*[♢◊]\s*$", "", text.strip())
    match = re.search(
        r"(?:(?P<source>[A-Z0-9/]{1,16})\s*:\s*)?"
        r"(?:(?P<destination>[A-Z0-9/]{1,16})\s+)?"
        r"YES\s+MSG\s+ID\s+(?P<message_id>\d+)\b",
        cleaned,
        re.IGNORECASE,
    )
    if match is None:
        return None
    try:
        value = int(match.group("message_id"))
    except ValueError:
        return None
    if not 0 <= value <= 2_147_483_647:
        return None
    source = match.group("source")
    destination = match.group("destination")
    return (
        source.upper() if source else None,
        destination.upper() if destination else None,
        value,
    )
