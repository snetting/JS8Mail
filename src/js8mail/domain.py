"""Small domain types shared by adapters and application services."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


def utc_now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    """A bounded event independent of the JS8Call wire representation."""

    event_type: str
    value: str
    params: Mapping[str, Any]
    received_at_ms: int

    @property
    def callsign(self) -> str | None:
        for key in ("CALL", "FROM", "TO"):
            value = self.params.get(key)
            if isinstance(value, str) and value:
                return value
        return None
