"""Conservative, deterministic radio delivery policies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

# JS8Call API speed identifiers are intentionally sparse.
SPEED_AIRTIME_MS = {
    0: 30_000,  # Normal
    1: 15_000,  # Fast
    2: 10_000,  # JS8-40 / Turbo
    4: 45_000,  # Slow
    8: 5_000,  # JS8-60 / Ultra (experimental)
}
SPEED_ORDER = (4, 0, 1, 2, 8)


class AirtimeBudgetExceeded(RuntimeError):
    """A local safety budget, rather than the radio, prevented transmission."""

    def __init__(self, scope: str, retry_at_ms: int | None = None) -> None:
        self.scope = scope
        self.retry_at_ms = retry_at_ms
        super().__init__(f"{scope} airtime budget exhausted")


def estimate_airtime_ms(text: str, speed: int) -> int:
    """Provisional airtime reserve, corrected upward from observed PTT time.

    JS8Call's token encoding and frame count are not exposed by this API;
    this estimate must never be used as proof that RF transmission ended.
    """
    if speed not in SPEED_AIRTIME_MS:
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

    # JS8Call's documented API identifiers ordered from slowest to fastest.
    SNR_MARGINS: ClassVar[dict[int, float]] = {
        4: -24.0,
        0: -18.0,
        1: -12.0,
        2: -12.0,
        8: -5.0,
    }

    def recommend(self, current: int, evidence: dict[int, SpeedEvidence]) -> SpeedDecision:
        if current not in SPEED_AIRTIME_MS:
            current = 0
        current_evidence = evidence.get(current, SpeedEvidence())
        if current_evidence.attempts >= 2 and current_evidence.reliability < 0.5:
            current_index = SPEED_ORDER.index(current)
            slower = SPEED_ORDER[max(0, current_index - 1)]
            return SpeedDecision(
                slower, slower != current, "recent failures justify stepping down one speed"
            )
        chosen = current
        for speed in reversed(SPEED_ORDER):
            item = evidence.get(speed, SpeedEvidence())
            if (
                item.successes >= 3
                and item.reliability >= 0.8
                and (item.average_snr is None or item.average_snr >= self.SNR_MARGINS[speed])
            ):
                chosen = speed
                break
        # API speed identifiers are sparse and are not ordered numerically;
        # compare their position in the policy order instead.
        if SPEED_ORDER.index(chosen) > SPEED_ORDER.index(current):
            return SpeedDecision(
                chosen, True, "sustained reliable evidence supports a cautious speed increase"
            )
        return SpeedDecision(
            current, False, "retain current speed until stronger evidence is available"
        )


@dataclass(slots=True)
class AirtimeBudget:
    """Rolling accounting for one scope; ``message_limit_ms`` is optional.

    A radio-wide budget is a rolling-window safety limit.  A per-message
    budget may additionally have a lifetime limit.  Keeping the lifetime
    limit optional prevents a legacy, persisted message counter from turning
    into a permanent station-wide TX lock.
    """

    window_limit_ms: int = 15 * 60 * 1000
    message_limit_ms: int | None = 5 * 60 * 1000
    window_used_ms: int = 0
    message_used_ms: int = 0
    window_started_at_ms: int | None = None
    window_duration_ms: int = 15 * 60 * 1000

    def rollover(self, now_ms: int) -> None:
        if self.window_started_at_ms is None:
            self.window_started_at_ms = now_ms
        elif now_ms - self.window_started_at_ms >= self.window_duration_ms:
            self.window_started_at_ms = now_ms
            self.window_used_ms = 0

    def next_available_at(self, airtime_ms: int, now_ms: int) -> int | None:
        """Return the earliest time this budget can admit ``airtime_ms``.

        ``None`` means that the optional lifetime limit can never admit the
        requested amount.  The returned time is intentionally precise so a
        scheduler does not convert a short budget wait into a fresh full
        window delay.
        """
        self.rollover(now_ms)
        if airtime_ms < 0:
            return None
        if (
            self.message_limit_ms is not None
            and self.message_used_ms + airtime_ms > self.message_limit_ms
        ):
            return None
        if self.window_used_ms + airtime_ms <= self.window_limit_ms:
            return now_ms
        if self.window_started_at_ms is None:
            return now_ms
        return self.window_started_at_ms + self.window_duration_ms

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
        return self.window_used_ms + airtime_ms <= self.window_limit_ms and (
            self.message_limit_ms is None
            or self.message_used_ms + airtime_ms <= self.message_limit_ms
        )

    def spend(self, airtime_ms: int) -> bool:
        if not self.can_spend(airtime_ms):
            return False
        self.window_used_ms += airtime_ms
        if self.message_limit_ms is not None:
            self.message_used_ms += airtime_ms
        return True

    def retry_delay_ms(self, retry_count: int, priority: int = 0) -> int:
        # Priority changes ordering, not the safety ceiling.
        base: int = 60_000 * (2 ** min(max(0, retry_count), 8))
        if priority >= 3:
            base //= 2
        return min(max(60_000, base), 6 * 60 * 60 * 1000)
