"""Conservative, deterministic radio delivery policies."""

from __future__ import annotations

from dataclasses import dataclass

SPEED_AIRTIME_MS = (30_000, 15_000, 10_000, 7_500, 5_000)


def estimate_airtime_ms(text: str, speed: int) -> int:
    """Conservative planning estimate for one JS8Call frame.

    JS8Call's exact occupied time is mode/cycle dependent; this deliberately
    overestimates and is used only for local duty-cycle protection.
    """
    if not 0 <= speed < len(SPEED_AIRTIME_MS):
        raise ValueError("unsupported JS8Call speed")
    return SPEED_AIRTIME_MS[speed] + max(0, len(text.encode("utf-8")) - 32) * 100


@dataclass(frozen=True, slots=True)
class SpeedEvidence:
    successes: int = 0
    failures: int = 0
    average_snr: float | None = None

    @property
    def attempts(self) -> int:
        return self.successes + self.failures

    @property
    def reliability(self) -> float:
        return self.successes / self.attempts if self.attempts else 0.0


@dataclass(frozen=True, slots=True)
class SpeedDecision:
    speed: int
    changed: bool
    explanation: str


class AdaptiveSpeedPolicy:
    """Choose a speed from evidence without oscillating between modes."""

    # JS8Call's documented ordering: Slow, Normal, Fast, Turbo, Ultra.
    SNR_MARGINS = (-24.0, -18.0, -12.0, -8.0, -5.0)

    def recommend(self, current: int, evidence: dict[int, SpeedEvidence]) -> SpeedDecision:
        current = max(0, min(4, current))
        current_evidence = evidence.get(current, SpeedEvidence())
        if current_evidence.attempts >= 2 and current_evidence.reliability < 0.5:
            slower = max(0, current - 1)
            return SpeedDecision(slower, slower != current, "recent failures justify stepping down one speed")
        chosen = current
        for speed in range(4, -1, -1):
            item = evidence.get(speed, SpeedEvidence())
            if item.successes >= 3 and item.reliability >= 0.8 and (
                item.average_snr is None or item.average_snr >= self.SNR_MARGINS[speed]
            ):
                chosen = speed
                break
        if chosen > current:
            return SpeedDecision(chosen, True, "sustained reliable evidence supports a cautious speed increase")
        return SpeedDecision(current, False, "retain current speed until stronger evidence is available")


@dataclass(slots=True)
class AirtimeBudget:
    """Rolling accounting for one daemon; callers persist decisions in audit."""

    window_limit_ms: int = 15 * 60 * 1000
    message_limit_ms: int = 5 * 60 * 1000
    window_used_ms: int = 0
    message_used_ms: int = 0
    window_started_at_ms: int | None = None

    def rollover(self, now_ms: int) -> None:
        if self.window_started_at_ms is None:
            self.window_started_at_ms = now_ms
        elif now_ms - self.window_started_at_ms >= 15 * 60 * 1000:
            self.window_started_at_ms = now_ms
            self.window_used_ms = 0

    def can_spend_at(self, airtime_ms: int, now_ms: int) -> bool:
        self.rollover(now_ms)
        return self.can_spend(airtime_ms)

    def spend_at(self, airtime_ms: int, now_ms: int) -> bool:
        if not self.can_spend_at(airtime_ms, now_ms):
            return False
        return self.spend(airtime_ms)

    def can_spend(self, airtime_ms: int) -> bool:
        if airtime_ms < 0:
            return False
        return (
            self.window_used_ms + airtime_ms <= self.window_limit_ms
            and self.message_used_ms + airtime_ms <= self.message_limit_ms
        )

    def spend(self, airtime_ms: int) -> bool:
        if not self.can_spend(airtime_ms):
            return False
        self.window_used_ms += airtime_ms
        self.message_used_ms += airtime_ms
        return True

    def retry_delay_ms(self, retry_count: int, priority: int = 0) -> int:
        # Priority changes ordering, not the safety ceiling.
        base: int = 60_000 * (2 ** min(max(0, retry_count), 8))
        if priority >= 3:
            base //= 2
        return min(max(60_000, base), 6 * 60 * 60 * 1000)
