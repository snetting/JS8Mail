"""Track a JS8Call transmit train across short inter-frame PTT gaps."""

from dataclasses import dataclass


TX_TRAIN_QUIET_MS = 15_000


@dataclass(slots=True)
class TxTrain:
    ptt_on: bool = False
    saw_ptt: bool = False
    last_activity_ms: int = 0
    off_at_ms: int | None = None
    on_at_ms: int | None = None
    on_air_ms: int = 0

    def ptt(self, on: bool, now_ms: int) -> None:
        if on:
            if not self.ptt_on:
                self.on_at_ms = now_ms
            self.ptt_on = True
            self.saw_ptt = True
            self.off_at_ms = None
            self.last_activity_ms = now_ms
        elif self.saw_ptt:
            if self.ptt_on and self.on_at_ms is not None:
                self.on_air_ms += max(0, now_ms - self.on_at_ms)
            self.on_at_ms = None
            self.ptt_on = False
            self.off_at_ms = now_ms
            self.last_activity_ms = now_ms

    def frame(self, now_ms: int) -> None:
        self.last_activity_ms = now_ms
        # Some builds report TX.FRAME after the final PTT-off. Extend the
        # settle interval without losing the only completion signal.
        if self.saw_ptt and not self.ptt_on:
            self.off_at_ms = now_ms

    def settled(self, now_ms: int) -> bool:
        return (
            self.saw_ptt
            and not self.ptt_on
            and self.off_at_ms is not None
            and now_ms - max(self.last_activity_ms, self.off_at_ms) >= TX_TRAIN_QUIET_MS
        )

    def reset(self) -> None:
        self.ptt_on = False
        self.saw_ptt = False
        self.last_activity_ms = 0
        self.off_at_ms = None
        self.on_at_ms = None
        self.on_air_ms = 0
