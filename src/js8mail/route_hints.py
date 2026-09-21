"""Optional, best-effort client for the JS8Mail route-hints service."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PROTOCOL = "j8rh/1"


@dataclass(slots=True)
class RouteHintsState:
    enabled: bool = False
    state: str = "disabled"
    last_pull_at_ms: int | None = None
    last_push_at_ms: int | None = None
    last_success_at_ms: int | None = None
    last_error: str = ""
    hints_received: int = 0
    claims_published: int = 0


class RouteHintsClient:
    """Non-authoritative network evidence client.

    All network I/O runs in a worker thread. A service timeout or malformed
    response becomes a status update rather than an exception in the radio
    scheduler.
    """

    def __init__(self, endpoint: str = "", timeout_seconds: float = 4.0) -> None:
        self.endpoint = endpoint.strip().rstrip("/")
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 20.0))
        self.state = RouteHintsState(
            enabled=bool(self.endpoint), state="ready" if self.endpoint else "disabled"
        )

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint) and self.state.enabled

    def set_enabled(self, enabled: bool) -> None:
        self.state.enabled = bool(enabled) and bool(self.endpoint)
        self.state.state = "ready" if self.state.enabled else "disabled"

    def public_status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "configured": bool(self.endpoint),
            "state": self.state.state,
            "last_pull_at_ms": self.state.last_pull_at_ms,
            "last_push_at_ms": self.state.last_push_at_ms,
            "last_success_at_ms": self.state.last_success_at_ms,
            "last_error": self.state.last_error,
            "hints_received": self.state.hints_received,
            "claims_published": self.state.claims_published,
        }

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        request = Request(
            self.endpoint + path,
            data=data,
            method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            value = json.loads(response.read(512_000))
        if not isinstance(value, dict):
            raise TypeError("route-hints service returned a non-object response")
        return value

    async def pull(self, target: str, band: str, limit: int = 100) -> list[dict[str, Any]]:
        if not self.enabled or not target or not band:
            return []
        now = int(time.time() * 1000)
        self.state.last_pull_at_ms = now
        try:
            query = urlencode(
                {"target": target.upper(), "band": band.lower(), "limit": max(1, min(limit, 100))}
            )
            result = await asyncio.to_thread(self._request, "GET", f"/v1/evidence?{query}")
            if result.get("protocol") != PROTOCOL:
                raise ValueError("unsupported route-hints protocol")
            evidence = result.get("evidence", [])
            if not isinstance(evidence, list):
                raise TypeError("invalid route-hints evidence list")
            hints = [item for item in evidence if isinstance(item, dict)]
            self.state.state = "online"
            self.state.last_success_at_ms = int(time.time() * 1000)
            self.state.last_error = ""
            self.state.hints_received += len(hints)
            return hints
        except (
            OSError,
            HTTPError,
            URLError,
            TimeoutError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            self.state.state = "offline"
            self.state.last_error = str(exc)[:160] or type(exc).__name__
            return []

    async def push(self, observer: str, claims: list[dict[str, Any]]) -> int:
        if not self.enabled or not observer or not claims:
            return 0
        now = int(time.time() * 1000)
        self.state.last_push_at_ms = now
        try:
            result = await asyncio.to_thread(
                self._request,
                "POST",
                "/v1/evidence/batch",
                {"protocol": PROTOCOL, "observer": observer.upper(), "claims": claims[:100]},
            )
            if result.get("protocol") != PROTOCOL:
                raise ValueError("unsupported route-hints protocol")
            accepted = int(result.get("accepted", 0))
            self.state.state = "online"
            self.state.last_success_at_ms = int(time.time() * 1000)
            self.state.last_error = ""
            self.state.claims_published += max(0, accepted)
            return max(0, accepted)
        except (
            OSError,
            HTTPError,
            URLError,
            TimeoutError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            self.state.state = "offline"
            self.state.last_error = str(exc)[:160] or type(exc).__name__
            return 0


def claims_from_observations(
    observations: list[dict[str, Any]], observer: str, *, limit: int = 100
) -> list[dict[str, Any]]:
    """Convert local observations into deliberately modest public claims."""
    result: list[dict[str, Any]] = []
    local = observer.strip().upper()
    for item in observations:
        params = item.get("params", {})
        source = str(params.get("FROM", "")).strip().upper()
        destination = str(params.get("TO", "")).strip().upper()
        band = str(item.get("band", "")).strip().lower()
        if not source or not band or source.startswith("@") or source == local:
            continue
        if destination.startswith("@") or destination == local:
            kind = "heard"
            destination = ""
        elif not destination or destination == source:
            continue
        else:
            kind = "observed_traffic"
        claim: dict[str, Any] = {
            "kind": kind,
            "source": source,
            "band": band,
            "observed_at_ms": int(item.get("observed_at_ms", 0)),
        }
        if destination:
            claim["destination"] = destination
        if isinstance(item.get("dial_frequency"), int):
            claim["dial_frequency"] = item["dial_frequency"]
        if isinstance(params.get("SNR"), (int, float)):
            claim["snr"] = params["SNR"]
        result.append(claim)
        if len(result) >= limit:
            break
    return result
