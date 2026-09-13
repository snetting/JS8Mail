"""Deterministic, local-only temporal route selection.

This is deliberately an evidence scorer, not a claim that a route is
guaranteed. Missing or old evidence lowers confidence; it never proves that a
station is absent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


class RouteAction(StrEnum):
    DIRECT = "direct"
    RELAY_NOW = "relay_now"
    STORE = "store"
    DEFER = "defer"


@dataclass(frozen=True, slots=True)
class LinkEvidence:
    source: str
    destination: str
    observed_at_ms: int
    base_score: float
    source_kind: str = "local"
    expected_airtime_ms: int = 1000
    available: bool = True

    def score(self, now_ms: int, half_life_ms: int = 86_400_000) -> float:
        age = max(0, now_ms - self.observed_at_ms)
        decay = math.exp(-math.log(2) * age / half_life_ms)
        return max(0.0, min(1.0, self.base_score * decay))


@dataclass(frozen=True, slots=True)
class RoutePlan:
    action: RouteAction
    path: tuple[str, ...]
    score: float
    weakest_link_score: float
    expected_airtime_ms: int
    explanation: str


class TemporalGraph:
    def __init__(self) -> None:
        self._links: dict[tuple[str, str], list[LinkEvidence]] = {}

    def add(self, evidence: LinkEvidence) -> None:
        key = (evidence.source.upper(), evidence.destination.upper())
        self._links.setdefault(key, []).append(evidence)

    def candidates(self, source: str, now_ms: int) -> list[tuple[str, LinkEvidence, float]]:
        source = source.upper()
        result: list[tuple[str, LinkEvidence, float]] = []
        for (left, right), links in self._links.items():
            if left != source:
                continue
            usable = [link for link in links if link.available]
            if not usable:
                continue
            link = max(usable, key=lambda item: item.score(now_ms))
            score = link.score(now_ms)
            if score > 0:
                result.append((right, link, score))
        return sorted(result, key=lambda item: (-item[2], item[0]))


class RouteEngine:
    def __init__(
        self, graph: TemporalGraph, *, max_hops: int = 4, minimum_score: float = 0.25
    ) -> None:
        self.graph = graph
        self.max_hops = max(1, min(max_hops, 8))
        self.minimum_score = max(0.0, min(minimum_score, 1.0))

    def choose(
        self,
        origin: str,
        destination: str,
        *,
        now_ms: int,
        attempted_paths: set[tuple[str, ...]] | None = None,
    ) -> RoutePlan:
        origin, destination = origin.upper(), destination.upper()
        attempted = attempted_paths or set()
        paths: list[RoutePlan] = []

        def walk(node: str, path: tuple[str, ...], scores: tuple[float, ...], airtime: int) -> None:
            if len(path) - 1 > self.max_hops:
                return
            if node == destination:
                if path not in attempted:
                    weakest = min(scores, default=0.0)
                    # Weakest-link score dominates; extra hops and airtime are
                    # tie-break penalties rather than hidden probability math.
                    total = weakest - 0.03 * (len(path) - 2) - min(airtime / 600_000, 0.2)
                    if total < self.minimum_score:
                        return
                    action = RouteAction.DIRECT if len(path) == 2 else RouteAction.RELAY_NOW
                    paths.append(RoutePlan(action, path, max(0.0, total), weakest, airtime, ""))
                return
            for next_node, link, score in self.graph.candidates(node, now_ms):
                if next_node in path:
                    continue
                walk(
                    next_node,
                    path + (next_node,),
                    scores + (score,),
                    airtime + link.expected_airtime_ms,
                )

        walk(origin, (origin,), (), 0)
        if not paths:
            return RoutePlan(
                RouteAction.DEFER,
                (origin,),
                0.0,
                0.0,
                0,
                f"No unattempted path from {origin} to {destination} within {self.max_hops} hops; defer and gather evidence.",
            )
        selected = max(
            paths, key=lambda plan: (plan.score, -len(plan.path), -plan.expected_airtime_ms)
        )
        explanation = (
            f"Selected {'direct' if selected.action is RouteAction.DIRECT else 'relay'} path "
            f"{' → '.join(selected.path)}; weakest link {selected.weakest_link_score:.2f}, "
            f"estimated airtime {selected.expected_airtime_ms} ms."
        )
        return RoutePlan(
            selected.action,
            selected.path,
            selected.score,
            selected.weakest_link_score,
            selected.expected_airtime_ms,
            explanation,
        )
