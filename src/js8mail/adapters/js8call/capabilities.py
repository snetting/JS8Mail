"""Capability facts for a single JS8Call connection."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class CapabilitySnapshot:
    """Capabilities are facts with provenance, never guessed from a version string."""

    source: str = "unknown"
    version: str | None = None
    supported: set[str] = field(default_factory=set)
    unsupported: set[str] = field(default_factory=set)
    unknown: set[str] = field(default_factory=set)

    def has(self, name: str) -> bool:
        return name in self.supported

    def mark(self, name: str, state: str) -> None:
        if state not in {"supported", "unsupported", "unknown"}:
            raise ValueError(f"Invalid capability state: {state}")
        self.supported.discard(name)
        self.unsupported.discard(name)
        self.unknown.discard(name)
        getattr(self, state).add(name)
