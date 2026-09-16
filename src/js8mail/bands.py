"""Amateur-band normalization for frequency-aware observations.

The exact dial frequency is retained as provenance, but routing uses the
normalized band.  This deliberately tolerates small VFO nudges within a band.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Broad amateur allocations, expressed in Hz.  The ranges are intentionally
# conservative and are used only to label observations, not to authorize TX.
BAND_RANGES: tuple[tuple[str, int, int], ...] = (
    ("160m", 1_800_000, 2_000_000),
    ("80m", 3_500_000, 4_000_000),
    ("60m", 5_250_000, 5_450_000),
    ("40m", 7_000_000, 7_300_000),
    ("30m", 10_100_000, 10_150_000),
    ("20m", 14_000_000, 14_350_000),
    ("17m", 18_068_000, 18_168_000),
    ("15m", 21_000_000, 21_450_000),
    ("12m", 24_890_000, 24_990_000),
    ("10m", 28_000_000, 29_700_000),
    ("6m", 50_000_000, 54_000_000),
)


def band_from_frequency_hz(frequency_hz: float | None) -> str:
    if frequency_hz is None:
        return ""
    try:
        frequency = int(frequency_hz)
    except (TypeError, ValueError):
        return ""
    for band, lower, upper in BAND_RANGES:
        if lower <= frequency <= upper:
            return band
    return ""


def context_from_params(params: Mapping[str, Any]) -> tuple[str, int | None]:
    """Extract band and raw dial frequency from JS8Call-style parameters."""
    explicit = params.get("BAND")
    dial = params.get("DIAL")
    if isinstance(dial, bool) or not isinstance(dial, (int, float)):
        dial = params.get("FREQ")
    raw_dial = int(dial) if isinstance(dial, (int, float)) and not isinstance(dial, bool) else None
    return (
        str(explicit).strip().lower() if explicit else band_from_frequency_hz(raw_dial),
        raw_dial,
    )
