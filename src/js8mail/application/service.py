"""Mailbox operations used by the local UI."""

from __future__ import annotations

import secrets
from typing import Any

from js8mail.application.lifecycle import MessageState
from js8mail.domain import utc_now_ms
from js8mail.routing import LinkEvidence, RouteEngine, RoutePlan, TemporalGraph
from js8mail.storage import Database

DEFAULT_MESSAGE_TTL_MS = 3 * 24 * 60 * 60 * 1000


class MailService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def compose(self, destination: str, subject: str, body: str, priority: int = 0) -> str:
        destination = destination.strip().upper()
        if not destination or len(destination) > 16:
            raise ValueError("destination must be 1–16 characters")
        if not body.strip() or len(body) > 4096:
            raise ValueError("body must contain 1–4096 characters")
        if not 0 <= priority <= 3:
            raise ValueError("priority must be between 0 and 3")
        message_id = secrets.token_hex(8)
        self.database.enqueue_message(
            message_id,
            destination,
            body,
            subject=subject.strip()[:120],
            priority=priority,
            expires_at_ms=utc_now_ms() + DEFAULT_MESSAGE_TTL_MS,
        )
        return message_id

    def cancel(self, message_id: str) -> None:
        self.database.transition_message(message_id, MessageState.CANCELLED)

    def retry(self, message_id: str) -> None:
        message = self.database.get_message(message_id)
        if message is None:
            raise KeyError(message_id)
        if message["state"] not in {MessageState.FAILED, MessageState.CANCELLED}:
            raise ValueError("only failed or cancelled messages can be retried")
        self.database.connection.execute(
            "UPDATE messages SET state = ?, updated_at_ms = ? WHERE id = ?",
            (MessageState.QUEUED, utc_now_ms(), message_id),
        )
        self.database.connection.commit()
        self.database.audit("message.requeued", {"message_id": message_id})

    def retry_now(self, message_id: str) -> None:
        message = self.database.get_message(message_id)
        if message is None:
            raise KeyError(message_id)
        if message["state"] not in {MessageState.QUEUED, MessageState.WAITING_ROUTE}:
            raise ValueError("only queued or waiting messages can be retried now")
        self.database.connection.execute(
            "UPDATE messages SET state = ?, next_attempt_at_ms = NULL, updated_at_ms = ? WHERE id = ?",
            (MessageState.QUEUED, utc_now_ms(), message_id),
        )
        self.database.connection.commit()
        self.database.record_attempt(message_id, "manual_retry", "route", "requested", "operator requested immediate retry")

    def delete(self, message_id: str) -> None:
        self.database.delete_message(message_id)

    def plan_route(
        self,
        origin: str,
        destination: str,
        now_ms: int | None = None,
        attempted_paths: set[tuple[str, ...]] | None = None,
        band: str | None = None,
    ) -> RoutePlan:
        now = utc_now_ms() if now_ms is None else now_ms
        graph = TemporalGraph()
        if band == "":
            return RouteEngine(graph).choose(
                origin, destination, now_ms=now, attempted_paths=attempted_paths
            )
        for link in self.database.temporal_link_views(5000):
            if band is not None and str(link.get("band", "")).lower() != band.strip().lower():
                continue
            snr = link.get("max_snr")
            snr_value = float(snr) if isinstance(snr, (int, float)) else -30.0
            score = max(0.25, min(1.0, 0.55 + (snr_value + 20.0) / 40.0))
            graph.add(
                LinkEvidence(
                    str(link["source"]),
                    str(link["destination"]),
                    int(link["last_observed_at_ms"]),
                    score,
                    expected_airtime_ms=1000,
                )
            )
        for observation in self.database.recent_observations(500, band=band):
            params = observation["params"]
            source = params.get("FROM")
            target = params.get("TO")
            if not isinstance(source, str) or not isinstance(target, str):
                continue
            if not source or not target or target.startswith("@"):
                continue
            snr = params.get("SNR", -30)
            snr_value = float(snr) if isinstance(snr, (int, float)) else -30.0
            score = max(0.25, min(1.0, 0.55 + (snr_value + 20.0) / 40.0))
            reported_age = params.get("AGE_MIN", 0)
            age_ms = int(reported_age) * 60_000 if isinstance(reported_age, int) else 0
            graph.add(
                LinkEvidence(
                    source,
                    target,
                    observation["observed_at_ms"] - age_ms,
                    score,
                    expected_airtime_ms=1000,
                )
            )
        return RouteEngine(graph).choose(
            origin, destination, now_ms=now, attempted_paths=attempted_paths
        )

    def message_views(self) -> list[dict[str, object]]:
        views: list[dict[str, object]] = []
        for message in self.database.list_messages():
            view = dict(message)
            view["attempts"] = self.database.list_attempts(str(message["id"]))
            view["custody"] = self.database.list_custody(str(message["id"]))
            attempts = view["attempts"]
            state = str(message["state"])
            if state == "cancelled":
                view["confidence"] = "cancelled"
            elif state == "failed":
                view["confidence"] = "delivery_failed"
            elif state == "expired":
                view["confidence"] = "expired"
            elif any(
                attempt["action"] == "delivery_ack" and attempt["status"] == "received"
                for attempt in attempts
            ):
                view["confidence"] = "delivered_to_js8mail"
            elif any(item["status"] == "accepted" for item in view["custody"]):
                view["confidence"] = "stored_at_custodian"
            elif any(
                attempt["action"] in {"hop_ack", "standard_ack"}
                and attempt["status"] == "received"
                for attempt in attempts
            ):
                view["confidence"] = "radio_acknowledged"
            elif any(
                attempt["action"] in {"direct", "relay", "store"}
                and attempt["status"] == "submitted"
                for attempt in attempts
            ):
                view["confidence"] = "submitted_to_js8call"
            elif any(attempt["action"] in {"snr_probe", "hearing_query", "allcall_query_call", "candidate_query_call"} for attempt in attempts):
                view["confidence"] = "discovery_in_progress"
            else:
                view["confidence"] = "uncertain"
            views.append(view)
        return views

    def message_graph(self, message_id: str, origin: str, band: str | None = None) -> dict[str, object]:
        message = self.database.get_message(message_id)
        if message is None:
            raise KeyError(message_id)
        destination = str(message["destination"]).upper()
        origin = origin.strip().upper()
        nodes: set[str] = {origin, destination}
        edges: dict[tuple[str, str], dict[str, Any]] = {}

        for observation in self.database.recent_observations(500, band=band):
            params = observation["params"]
            source, target = params.get("FROM"), params.get("TO")
            if not isinstance(source, str) or not isinstance(target, str):
                continue
            source, target = source.upper(), target.upper()
            # A self-directed test (or a JS8Call echo) is not a usable RF
            # link and makes the route graph misleading.
            if target.startswith("@") or source.startswith("@") or source == target:
                continue
            nodes.update((source, target))
            key = (source, target)
            edge = edges.setdefault(
                key,
                {"from": source, "to": target, "kind": "observed", "count": 0, "latest": 0, "snr": None},
            )
            edge["count"] = int(edge["count"]) + 1
            edge["latest"] = max(int(edge["latest"]), int(observation["observed_at_ms"]))
            snr = params.get("SNR")
            if isinstance(snr, (int, float)):
                edge["snr"] = snr if edge["snr"] is None else max(float(edge["snr"]), float(snr))

        for attempt in self.database.list_attempts(message_id):
            target = str(attempt["target"]).upper()
            if target.startswith("@") or target == "ROUTE" or target == origin:
                continue
            nodes.add(target)
            key = (origin, target)
            edge = edges.setdefault(
                key,
                {"from": origin, "to": target, "kind": "attempted", "count": 0, "latest": 0, "snr": None},
            )
            if attempt["status"] in {"received", "available"}:
                edge["kind"] = "confirmed"
            elif edge["kind"] == "observed":
                edge["kind"] = "attempted"

        return {
            "message_id": message_id,
            "origin": origin,
            "destination": destination,
            "nodes": sorted(nodes),
            "edges": list(edges.values()),
        }

    def live_activity_graph(
        self, band: str, *, max_age_ms: int = 2 * 60 * 60 * 1000
    ) -> dict[str, object]:
        """Return a compact, quickly aging graph for the live UI panel."""
        now = utc_now_ms()
        nodes: set[str] = set()
        edges: list[dict[str, object]] = []
        for link in self.database.temporal_link_views(500):
            if band == "" or str(link.get("band", "")) != band:
                continue
            age_ms = max(0, now - int(link["last_observed_at_ms"]))
            if age_ms > max_age_ms:
                continue
            source = str(link["source"])
            destination = str(link["destination"])
            nodes.update((source, destination))
            freshness = max(0.05, 1.0 - age_ms / max_age_ms)
            successes = int(link.get("success_count", 0))
            failures = int(link.get("failure_count", 0))
            kind = "confirmed" if successes else ("attempted" if failures else "observed")
            edges.append({
                "from": source,
                "to": destination,
                "kind": kind,
                "age_seconds": age_ms // 1000,
                "freshness": round(freshness, 3),
                "snr": link.get("max_snr"),
                "observations": int(link.get("observation_count", 0)),
            })
        return {"band": band, "generated_at_ms": now, "nodes": sorted(nodes), "edges": edges}

    def station_views(self, limit: int = 30, band: str | None = None) -> list[dict[str, object]]:
        """Summarise recent direct and remotely reported callsigns."""
        now = utc_now_ms()
        stations: dict[str, dict[str, Any]] = {}
        for observation in self.database.recent_observations(500, band=band):
            params = observation["params"]
            source = params.get("FROM")
            target = params.get("TO")
            snr = params.get("SNR")
            observed = int(observation["observed_at_ms"])
            if isinstance(source, str) and source and not source.startswith("@"):
                call = source.upper()
                item = stations.setdefault(
                    call,
                    {"callsign": call, "last_seen_ms": observed, "snr": None, "evidence": set()},
                )
                if observed >= int(item["last_seen_ms"]):
                    item["last_seen_ms"] = observed
                if isinstance(snr, (int, float)):
                    item["snr"] = snr if item["snr"] is None else max(float(item["snr"]), float(snr))
                evidence = item["evidence"]
                assert isinstance(evidence, set)
                evidence.add("direct" if observation["event_type"] != "QUERY.CALL.RESPONSE" else "remote_report")
            if isinstance(target, str) and target and not target.startswith("@"):
                call = target.upper()
                item = stations.setdefault(
                    call,
                    {"callsign": call, "last_seen_ms": observed, "snr": None, "evidence": set()},
                )
                if observed >= int(item["last_seen_ms"]):
                    item["last_seen_ms"] = observed
                if isinstance(snr, (int, float)):
                    item["snr"] = snr if item["snr"] is None else max(float(item["snr"]), float(snr))
                evidence = item["evidence"]
                assert isinstance(evidence, set)
                evidence.add("reported_target")
        result: list[dict[str, Any]] = []
        for item in stations.values():
            item["age_seconds"] = max(0, (now - int(item["last_seen_ms"])) // 1000)
            item["evidence"] = sorted(item["evidence"])
            result.append(item)
        result.sort(key=lambda item: (int(item["age_seconds"]), str(item["callsign"])))
        return result[: max(1, min(limit, 100))]

    def recently_heard(
        self, callsign: str, now_ms: int | None = None, window_ms: int = 600_000,
        band: str | None = None,
    ) -> bool:
        now = utc_now_ms() if now_ms is None else now_ms
        wanted = callsign.strip().upper()
        for observation in self.database.recent_observations(500, band=band):
            source = observation["params"].get("FROM")
            if (
                isinstance(source, str)
                and source.upper() == wanted
                and now - int(observation["observed_at_ms"]) <= window_ms
            ):
                return True
        return False

    def recently_answered(
        self,
        callsign: str,
        local_callsign: str,
        now_ms: int | None = None,
        window_ms: int = 600_000,
        band: str | None = None,
    ) -> bool:
        """Return true only for a recent directed response to this station."""
        now = utc_now_ms() if now_ms is None else now_ms
        wanted = callsign.strip().upper()
        local = local_callsign.strip().upper()
        if not wanted or not local:
            return False
        for observation in self.database.recent_observations(500, band=band):
            params = observation["params"]
            source = params.get("FROM")
            target = params.get("TO")
            addressed_event = observation["event_type"] in {"RX.DIRECTED.ME", "RX.DIRECTED"}
            if (
                addressed_event
                and isinstance(source, str)
                and source.upper() == wanted
                and (not isinstance(target, str) or target.upper() == local)
                and now - int(observation["observed_at_ms"]) <= window_ms
            ):
                return True
        return False

    def promising_stations(
        self, destination: str, now_ms: int | None = None, window_ms: int = 600_000,
        band: str | None = None,
    ) -> list[str]:
        wanted = destination.strip().upper()
        now = utc_now_ms() if now_ms is None else now_ms
        scores: dict[str, float] = {}
        for observation in self.database.recent_observations(500, band=band):
            if now - int(observation["observed_at_ms"]) > window_ms:
                continue
            params = observation["params"]
            source, target = params.get("FROM"), params.get("TO")
            if (
                not isinstance(source, str)
                or not isinstance(target, str)
                or target.upper() != wanted
            ):
                continue
            snr = params.get("SNR", -30)
            value = float(snr) if isinstance(snr, (int, float)) else -30.0
            scores[source.upper()] = max(scores.get(source.upper(), 0.0), value)
        return [
            station
            for station, _ in sorted(
                scores.items(),
                key=lambda item: (-item[1], -self.database.custodian_score(item[0]), item[0]),
            )
        ]
