"""Mailbox operations used by the local UI."""

from __future__ import annotations

import secrets
from typing import Any

from js8mail.application.lifecycle import MessageState
from js8mail.domain import utc_now_ms
from js8mail.routing import LinkEvidence, RouteEngine, RoutePlan, TemporalGraph
from js8mail.storage import Database

DEFAULT_MESSAGE_TTL_MS = 3 * 24 * 60 * 60 * 1000
ENHANCED_MODES = frozenset({"standard", "opportunistic", "required"})


class MailService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def compose(
        self, destination: str, subject: str, body: str, priority: int = 0,
        enhanced_mode: str | None = None,
    ) -> str:
        destination = destination.strip().upper()
        if not destination or len(destination) > 16:
            raise ValueError("destination must be 1–16 characters")
        # Leave room for the JS8Call directed-message envelope (destination,
        # command, and the optional first-contact version marker). Enhanced
        # peers may still use multipart delivery for long bodies.
        if not body.strip() or len(body.encode("utf-8")) > 4000:
            raise ValueError("body must contain 1–4000 UTF-8 bytes")
        if not 0 <= priority <= 3:
            raise ValueError("priority must be between 0 and 3")
        if enhanced_mode is not None and enhanced_mode not in ENHANCED_MODES:
            raise ValueError("invalid JS8M mode")
        # JS8Call groups are broadcast destinations, not a single enhanced
        # endpoint.  Keep their wire format ordinary and human-readable;
        # capability negotiation and JS8Mail multipart framing are only for
        # directed station delivery.
        if destination.startswith("@"):
            enhanced_mode = "standard"
        message_id = secrets.token_hex(8)
        self.database.enqueue_message(
            message_id,
            destination,
            body,
            subject=subject.strip()[:120],
            priority=priority,
            expires_at_ms=utc_now_ms() + DEFAULT_MESSAGE_TTL_MS,
            enhanced_mode=enhanced_mode,
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

        def score_from_snr(snr: object, evidence: str = "historical") -> float:
            if isinstance(snr, (int, float)):
                # -20 dB is useful but uncertain; -5 dB is strong. Do not
                # floor weak/unknown links into viable routes.
                base = 0.35 + (float(snr) + 20.0) / 50.0
            else:
                base = 0.42 if evidence in {"query_answered", "remote_query_call_yes"} else 0.32
            if evidence == "remote_query_call_yes":
                base *= 0.8  # reported reachability, not a local ACK
            return max(0.0, min(1.0, base))

        if band == "":
            return RouteEngine(graph).choose(
                origin, destination, now_ms=now, attempted_paths=attempted_paths
            )
        for link in self.database.temporal_link_views(5000):
            if band is not None and str(link.get("band", "")).lower() != band.strip().lower():
                continue
            snr = link.get("max_snr")
            score = score_from_snr(snr, "historical")
            graph.add(
                LinkEvidence(
                    str(link["source"]),
                    str(link["destination"]),
                    int(link["last_observed_at_ms"]),
                    score,
                    source_kind="historical",
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
            snr = params.get("SNR")
            evidence = str(params.get("EVIDENCE", "local"))
            score = score_from_snr(snr, evidence)
            reported_age = params.get("AGE_MIN", 0)
            age_ms = int(reported_age) * 60_000 if isinstance(reported_age, int) else 0
            graph.add(
                LinkEvidence(
                    source,
                    target,
                    observation["observed_at_ms"] - age_ms,
                    score,
                    source_kind="remote" if evidence == "remote_query_call_yes" else "local",
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
            view["transmissions"] = self.database.list_transmission_transactions(str(message["id"]))
            attempts = view["attempts"]
            state = str(message["state"])
            if state == "cancelled":
                view["confidence"] = "cancelled"
            elif state == "failed":
                view["confidence"] = "delivery_failed"
            elif state == "expired":
                view["confidence"] = "expired"
            elif state == "stored":
                view["confidence"] = "stored_at_custodian"
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
            else:
                operations = [
                    attempt
                    for attempt in attempts
                    if attempt["action"] in {"direct", "multipart", "relay", "store"}
                ]
                latest_operation = max(
                    operations,
                    key=lambda attempt: (int(attempt["created_at_ms"]), int(attempt["id"])),
                    default=None,
                )
                latest_attempt = max(
                    attempts,
                    key=lambda attempt: (int(attempt["created_at_ms"]), int(attempt["id"])),
                    default=None,
                )
                if latest_operation is not None and latest_attempt is not None:
                    if latest_attempt["status"] == "submitted" and latest_attempt["action"] in {
                        "direct", "multipart", "relay", "store"
                    }:
                        view["confidence"] = (
                            "awaiting_custodian_ack"
                            if latest_attempt["action"] == "store"
                            else "awaiting_delivery_ack"
                        )
                    elif latest_attempt["action"] in {
                        "delivery_timeout", "store_timeout", "relay_forward_timeout"
                    } or latest_attempt["status"] in {"uncertain", "deferred"}:
                        view["confidence"] = "delivery_uncertain"
                    elif latest_operation["status"] == "submitted":
                        view["confidence"] = "submitted_to_js8call"
                    else:
                        view["confidence"] = "delivery_uncertain"
                elif any(
                    attempt["action"] in {
                        "snr_probe", "hearing_query", "allcall_query_call", "candidate_query_call"
                    }
                    for attempt in attempts
                ):
                    view["confidence"] = "discovery_in_progress"
                elif state == MessageState.QUEUED:
                    view["confidence"] = "new"
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
        """Return a compact graph classified by recent reciprocal evidence."""
        now = utc_now_ms()
        nodes: set[str] = set()
        directions: dict[tuple[str, str], dict[str, Any]] = {}
        for observation in self.database.recent_observations(2000, band=band):
            age_ms = max(0, now - int(observation["observed_at_ms"]))
            if age_ms > max_age_ms:
                continue
            params = observation["params"]
            source = params.get("FROM")
            destination = params.get("TO")
            if not isinstance(source, str) or not isinstance(destination, str):
                continue
            source = source.strip().upper()
            destination = destination.strip().upper()
            if not source or not destination or source.startswith("@") or destination.startswith("@"):
                continue
            nodes.update((source, destination))
            key = (source, destination)
            item = directions.setdefault(key, {"latest": 0, "count": 0, "active": 0, "js8m": False, "snr": None})
            item["latest"] = max(int(item["latest"]), int(observation["observed_at_ms"]))
            item["count"] = int(item["count"]) + 1
            if age_ms <= 10 * 60 * 1000:
                item["active"] = int(item["active"]) + 1
            text = f"{observation['value']} {params.get('TEXT', '')}".upper()
            item["js8m"] = bool(item["js8m"] or "J8M" in text or "JS8MAIL" in text)
            snr = params.get("SNR")
            if isinstance(snr, (int, float)):
                item["snr"] = snr if item["snr"] is None else max(float(item["snr"]), float(snr))
        edges: list[dict[str, object]] = []
        pairs: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for source, destination in directions:
            pair = (source, destination) if source < destination else (destination, source)
            pairs.setdefault(pair, []).append((source, destination))
        for (left, right), directed_keys in pairs.items():
            forward = directions.get((left, right), {})
            reverse = directions.get((right, left), {})
            latest = max(int(forward.get("latest", 0)), int(reverse.get("latest", 0)))
            age_ms = max(0, now - latest)
            freshness = max(0.05, 1.0 - age_ms / max_age_ms)
            reciprocal = bool(forward and reverse and min(int(forward["latest"]), int(reverse["latest"])) >= now - 10 * 60 * 1000)
            active_count = max(int(forward.get("active", 0)), int(reverse.get("active", 0)))
            kind = "reciprocal" if reciprocal else ("active_one_way" if active_count >= 2 else "isolated_one_way")
            js8m = bool(forward.get("js8m", False) or reverse.get("js8m", False))
            edges.append({
                "from": left,
                "to": right,
                "kind": kind,
                "age_seconds": age_ms // 1000,
                "freshness": round(freshness, 3),
                "snr": forward.get("snr") if forward.get("snr") is not None else reverse.get("snr"),
                "observations": sum(int(directions[key]["count"]) for key in directed_keys),
                "js8m": js8m,
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
                    {
                        "callsign": call,
                        "last_seen_ms": observed,
                        "snr": None,
                        "evidence": set(),
                        "js8m": False,
                    },
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
                    {
                        "callsign": call,
                        "last_seen_ms": observed,
                        "snr": None,
                        "evidence": set(),
                        "js8m": False,
                    },
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
            item["js8m"] = self.database.peer_capabilities(str(item["callsign"])) is not None
            item["evidence"] = sorted(item["evidence"])
            result.append(item)
        result.sort(key=lambda item: (int(item["age_seconds"]), str(item["callsign"])))
        return result[: max(1, min(limit, 100))]

    def recently_heard(
        self, callsign: str, now_ms: int | None = None, window_ms: int = 600_000,
        band: str | None = None,
    ) -> bool:
        return self.recent_heard_age_ms(callsign, now_ms, window_ms, band) is not None

    def is_js8m_capable(self, callsign: str) -> bool:
        """Return whether a peer has a currently valid JS8Mail capability record."""
        return self.database.peer_capabilities(callsign.strip()) is not None

    def recent_heard_age_ms(
        self, callsign: str, now_ms: int | None = None, window_ms: int = 600_000,
        band: str | None = None,
    ) -> int | None:
        """Return the age of the freshest recent direct observation, if any.

        Hearing a station is probabilistic evidence that it may hear us too.
        It can justify a cautious direct attempt, but never proves delivery.
        """
        now = utc_now_ms() if now_ms is None else now_ms
        wanted = callsign.strip().upper()
        freshest: int | None = None
        for observation in self.database.recent_observations(500, band=band):
            source = observation["params"].get("FROM")
            # A remote QUERY CALL report says that somebody heard the target;
            # it is valuable route evidence, but it is not local hearing
            # evidence for a direct payload attempt.
            if (
                observation["event_type"] != "QUERY.CALL.RESPONSE"
                and isinstance(source, str)
                and source.upper() == wanted
            ):
                age = now - int(observation["observed_at_ms"])
                if 0 <= age <= window_ms and (freshest is None or age < freshest):
                    freshest = age
        return freshest

    def recently_answered(
        self,
        callsign: str,
        local_callsign: str,
        now_ms: int | None = None,
        window_ms: int = 600_000,
        band: str | None = None,
    ) -> bool:
        return self.recent_answered_age_ms(callsign, local_callsign, now_ms, window_ms, band) is not None

    def recent_answered_age_ms(
        self,
        callsign: str,
        local_callsign: str,
        now_ms: int | None = None,
        window_ms: int = 600_000,
        band: str | None = None,
    ) -> int | None:
        """Return the age of the freshest recent directed response."""
        now = utc_now_ms() if now_ms is None else now_ms
        wanted = callsign.strip().upper()
        local = local_callsign.strip().upper()
        if not wanted or not local:
            return None
        freshest: int | None = None
        for observation in self.database.recent_observations(500, band=band):
            params = observation["params"]
            source = params.get("FROM")
            target = params.get("TO")
            addressed_event = observation["event_type"] in {"RX.DIRECTED.ME", "RX.DIRECTED"}
            age = now - int(observation["observed_at_ms"])
            if (
                addressed_event
                and isinstance(source, str)
                and source.upper() == wanted
                and (not isinstance(target, str) or target.upper() == local)
                and 0 <= age <= window_ms
                and (freshest is None or age < freshest)
            ):
                freshest = age
        return freshest

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
            scores[source.upper()] = max(scores.get(source.upper(), -100.0), value)
        return [
            station
            for station, _ in sorted(
                scores.items(),
                key=lambda item: (-item[1], -self.database.custodian_score(item[0]), item[0]),
            )
        ]
