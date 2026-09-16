"""Conservative transmit eligibility decisions.

This module does not submit anything to JS8Call. It decides whether an exact
intent has passed the safety gates; a future adapter submission method will be
separate and capability-gated.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AutomationMode(StrEnum):
    OBSERVE = "observe"
    APPROVE = "approve"
    AUTOMATIC = "automatic"


@dataclass(frozen=True, slots=True)
class RadioSnapshot:
    connected: bool
    manual_text_empty: bool | None
    ptt_active: bool | None
    channel_busy: bool | None
    js8_queue_empty: bool | None = None


@dataclass(frozen=True, slots=True)
class TransmitIntent:
    action_id: str
    text: str
    dry_run: bool = False
    approved: bool = False


@dataclass(frozen=True, slots=True)
class ArbiterDecision:
    allowed: bool
    reason: str


class TransmitArbiter:
    def __init__(self, mode: AutomationMode = AutomationMode.OBSERVE) -> None:
        self.mode = mode
        self.paused = False

    def decide(self, intent: TransmitIntent, radio: RadioSnapshot) -> ArbiterDecision:
        if not intent.text or len(intent.text.encode("utf-8")) > 4096:
            return ArbiterDecision(False, "invalid or oversized transmission text")
        if intent.dry_run:
            return ArbiterDecision(True, "dry-run: no radio submission permitted")
        if self.paused:
            return ArbiterDecision(False, "automation is paused")
        if self.mode == AutomationMode.OBSERVE:
            return ArbiterDecision(False, "observe mode never submits transmissions")
        if self.mode == AutomationMode.APPROVE and not intent.approved:
            return ArbiterDecision(False, "operator approval required")
        if not radio.connected:
            return ArbiterDecision(False, "JS8Call is not connected")
        if radio.manual_text_empty is not True:
            return ArbiterDecision(False, "manual JS8Call compose state is not known empty")
        if radio.ptt_active is not False:
            return ArbiterDecision(False, "PTT state is not known inactive")
        if radio.channel_busy is not False:
            return ArbiterDecision(False, "channel state is not known idle")
        if radio.js8_queue_empty is False:
            return ArbiterDecision(False, "JS8Call transmit queue is occupied")
        return ArbiterDecision(True, "all current safety gates passed")
