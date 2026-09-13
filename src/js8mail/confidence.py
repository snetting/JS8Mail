"""Delivery confidence as evidence, not a fabricated probability."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class ConfidenceLevel(IntEnum):
    NONE = 0
    SUBMITTED = 1
    FRAMES_OBSERVED = 2
    HOP_ACKNOWLEDGED = 3
    PARTS_COMPLETE = 4
    END_TO_END = 5
    READ = 6


@dataclass(frozen=True, slots=True)
class DeliveryEvidence:
    submitted: bool = False
    frames_observed: int = 0
    hop_acknowledged: bool = False
    parts_complete: bool = False
    end_to_end_receipt: bool = False
    read_receipt: bool = False

    def level(self, *, enhanced_peer: bool) -> ConfidenceLevel:
        if self.read_receipt:
            return ConfidenceLevel.READ
        if self.end_to_end_receipt and enhanced_peer:
            return ConfidenceLevel.END_TO_END
        if self.parts_complete and enhanced_peer:
            return ConfidenceLevel.PARTS_COMPLETE
        if self.hop_acknowledged:
            return ConfidenceLevel.HOP_ACKNOWLEDGED
        if self.frames_observed:
            return ConfidenceLevel.FRAMES_OBSERVED
        if self.submitted:
            return ConfidenceLevel.SUBMITTED
        return ConfidenceLevel.NONE


def describe(evidence: DeliveryEvidence, *, enhanced_peer: bool) -> str:
    messages = {
        ConfidenceLevel.NONE: "Not transmitted",
        ConfidenceLevel.SUBMITTED: "Submitted to JS8Call; RF outcome not yet proven",
        ConfidenceLevel.FRAMES_OBSERVED: "Transmit frames observed; destination receipt not proven",
        ConfidenceLevel.HOP_ACKNOWLEDGED: "A station acknowledged a hop; end-to-end delivery not proven",
        ConfidenceLevel.PARTS_COMPLETE: "All enhanced message parts received; delivery receipt not yet proven",
        ConfidenceLevel.END_TO_END: "Destination JS8Mail client confirmed delivery",
        ConfidenceLevel.READ: "Destination operator opted in and confirmed reading",
    }
    level = evidence.level(enhanced_peer=enhanced_peer)
    if not enhanced_peer and level > ConfidenceLevel.HOP_ACKNOWLEDGED:
        level = ConfidenceLevel.HOP_ACKNOWLEDGED
    return messages[level]
