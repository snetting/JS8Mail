"""Mailbox operations used by the local UI."""

from __future__ import annotations

import json
import re
import secrets
from itertools import pairwise
from typing import Any

from js8mail.application.lifecycle import MessageState
from js8mail.domain import utc_now_ms
from js8mail.routing import LinkEvidence, RouteEngine, RoutePlan, TemporalGraph
from js8mail.storage import Database

MESSAGE_GRAPH_EVIDENCE_MAX_AGE_MS = 48 * 60 * 60 * 1000
MESSAGE_GRAPH_MAX_PATHS = 64
MESSAGE_GRAPH_MAX_OPERATION_EDGES = 80

DEFAULT_MESSAGE_TTL_MS = 3 * 24 * 60 * 60 * 1000
ENHANCED_MODES = frozenset({"standard", "opportunistic", "required"})


class MailService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def compose(
        self,
        destination: str,
        subject: str,
        body: str,
        priority: int = 0,
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
        message = self.database.get_message(message_id)
        if message is None:
            # The browser may have acted on a row removed by another refresh
            # or operator action. Cancellation is intentionally idempotent.
            return
        state = MessageState(str(message["state"]))
        if state in {
            MessageState.ACKNOWLEDGED,
            MessageState.STORED,
            MessageState.DELIVERED,
            MessageState.FAILED,
            MessageState.EXPIRED,
            MessageState.CANCELLED,
        }:
            return
        self.database.transition_message(message_id, MessageState.CANCELLED)

    def retry(self, message_id: str) -> None:
        message = self.database.get_message(message_id)
        if message is None:
            raise KeyError(message_id)
        if message["state"] not in {
            MessageState.ACKNOWLEDGED,
            MessageState.FAILED,
            MessageState.CANCELLED,
        }:
            raise ValueError("only acknowledged, failed, or cancelled messages can be retried")
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
        if message["state"] not in {
            MessageState.ACKNOWLEDGED,
            MessageState.QUEUED,
            MessageState.WAITING_ROUTE,
        }:
            raise ValueError("only acknowledged, queued, or waiting messages can be retried now")
        self.database.connection.execute(
            "UPDATE messages SET state = ?, next_attempt_at_ms = NULL, updated_at_ms = ? WHERE id = ?",
            (MessageState.QUEUED, utc_now_ms(), message_id),
        )
        self.database.connection.commit()
        self.database.record_attempt(
            message_id, "manual_retry", "route", "requested", "operator requested immediate retry"
        )

    def delete(self, message_id: str) -> None:
        try:
            self.database.delete_message(message_id)
        except KeyError:
            # Removing an already-removed row is a successful end state from
            # the operator's perspective, especially after a stale refresh.
            return

    def plan_route(
        self,
        origin: str,
        destination: str,
        now_ms: int | None = None,
        attempted_paths: set[tuple[str, ...]] | None = None,
        blocked_paths: set[tuple[str, ...]] | None = None,
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
            if evidence.startswith("remote_"):
                base *= 0.8  # reported reachability, not a local ACK
            return max(0.0, min(1.0, base))

        if band == "":
            return RouteEngine(graph).choose(
                origin,
                destination,
                now_ms=now,
                attempted_paths=attempted_paths,
                blocked_paths=blocked_paths,
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
                    source_kind="remote" if evidence.startswith("remote_") else "local",
                    expected_airtime_ms=1000,
                )
            )
        return RouteEngine(graph).choose(
            origin,
            destination,
            now_ms=now,
            attempted_paths=attempted_paths,
            blocked_paths=blocked_paths,
        )

    def blocked_message_paths(
        self, message_id: str, *, now_ms: int | None = None
    ) -> set[tuple[str, ...]]:
        return {
            tuple(path)
            for path, policy in self.database.message_route_failure_policies(
                message_id, now_ms=now_ms
            ).items()
            if bool(policy["blocked"])
        }

    def message_views(self, *, attempt_limit: int | None = 80) -> list[dict[str, object]]:
        """Return mailbox rows without expanding unbounded attempt history.

        Attempt history remains durable in SQLite and is still used by graph
        and diagnostic code. The normal mailbox refresh only needs a bounded
        recent window; sending thousands of rows to the browser every few
        seconds can otherwise make a long-lived retry loop unusable.
        """
        views: list[dict[str, object]] = []
        for message in self.database.list_messages():
            view = dict(message)
            all_attempts = self.database.list_attempts(str(message["id"]))
            view["attempts_total"] = len(all_attempts)
            if attempt_limit is None:
                attempts = all_attempts
            else:
                bounded_limit = max(1, int(attempt_limit))
                attempts = all_attempts[-bounded_limit:]
            view["attempts_truncated"] = len(attempts) < len(all_attempts)
            view["attempts"] = attempts
            custody = self.database.list_custody(str(message["id"]))
            transmissions = self.database.list_transmission_transactions(str(message["id"]))
            view["custody"] = custody[-80:]
            view["transmissions"] = transmissions[-80:]
            # Confidence still uses the complete durable history.
            attempts = all_attempts
            enhanced_outgoing = bool(
                self.database.list_message_parts(
                    str(message["id"]),
                    direction="outgoing",
                    peer=str(message["destination"]),
                )
            )
            state = str(message["state"])
            if state == "cancelled":
                view["confidence"] = "cancelled"
            elif state == "failed":
                view["confidence"] = "delivery_failed"
            elif state == "expired":
                view["confidence"] = "expired"
            elif state == "acknowledged":
                view["confidence"] = "enhanced_acknowledged_stopped"
            elif state == "delivered" and str(message["destination"]).upper().startswith("@"):
                view["confidence"] = "broadcast_submitted"
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
                attempt["action"] in {"hop_ack", "standard_ack"} and attempt["status"] == "received"
                for attempt in attempts
            ):
                # A plain JS8Call ACK can acknowledge an enhanced JS8Mail
                # frame without proving that the JS8Mail peer reassembled
                # the message. Do not expose that as "Standard" in the UI;
                # the sender still needs a JS8Mail receipt.
                view["confidence"] = (
                    "enhanced_acknowledged" if enhanced_outgoing else "radio_acknowledged"
                )
            else:
                latest_attempt = max(
                    attempts,
                    key=lambda attempt: (int(attempt["created_at_ms"]), int(attempt["id"])),
                    default=None,
                )
                if latest_attempt is not None:
                    action = str(latest_attempt["action"])
                    status = str(latest_attempt["status"])
                    delivery_actions = {"direct", "multipart", "relay", "store"}
                    discovery_actions = {
                        "snr_probe",
                        "route_probe",
                        "hearing_query",
                        "allcall_query_call",
                        "candidate_query_call",
                        "route_evidence",
                        "route_evidence_settling",
                        "manual_retry",
                        "reconcile",
                        "capability_wait",
                        "capability_timeout",
                    }
                    detail = str(latest_attempt.get("detail") or "").lower()
                    discovery_defer = action == "defer" and any(
                        word in detail
                        for word in ("route", "query", "probe", "discovery", "listening")
                    )
                    if status == "submitted" and action in delivery_actions:
                        view["confidence"] = (
                            "awaiting_custodian_ack"
                            if action == "store"
                            else "awaiting_delivery_ack"
                        )
                    elif action in {
                        "delivery_timeout",
                        "store_timeout",
                        "relay_forward_timeout",
                    } or (status in {"uncertain", "deferred"} and not discovery_defer):
                        view["confidence"] = "delivery_uncertain"
                    elif action in discovery_actions or action == "defer" or discovery_defer:
                        view["confidence"] = "discovery_in_progress"
                    elif action in delivery_actions:
                        view["confidence"] = "delivery_uncertain"
                    else:
                        view["confidence"] = "uncertain"
                elif state == MessageState.QUEUED:
                    view["confidence"] = "new"
                else:
                    view["confidence"] = "uncertain"
            views.append(view)
        return views

    def message_graph(
        self, message_id: str, origin: str, band: str | None = None
    ) -> dict[str, object]:
        """Build a semantic graph of RF evidence and this message's attempts.

        Observations and route reports are not delivery confirmations.  Keep
        those concepts separate from durable transmission outcomes so a
        station that merely reported hearing the destination cannot make an
        unacknowledged custody attempt appear green.
        """
        message = self.database.get_message(message_id)
        if message is None:
            raise KeyError(message_id)
        destination = str(message["destination"]).upper()
        message_state = str(message["state"])
        origin = origin.strip().upper()
        now = utc_now_ms()
        nodes: set[str] = {origin, destination}
        edges: dict[tuple[str, str], dict[str, Any]] = {}

        def edge_for(source: str, target: str) -> dict[str, Any]:
            key = (source, target)
            return edges.setdefault(
                key,
                {
                    "from": source,
                    "to": target,
                    "kind": "observed",
                    "count": 0,
                    "latest": 0,
                    "snr": None,
                    "attempts": 0,
                    "last_action": None,
                    "last_status": None,
                    "label": "observed",
                    "network_hint": False,
                    "rf_observed": False,
                },
            )

        # Higher-priority operation outcomes replace passive evidence on the
        # same pair, while preserving counters and RF SNR information.
        priority = {
            "observed": 0,
            "reported": 1,
            # A submitted-but-unacknowledged operation is still actionable
            # evidence, even if an earlier API handoff failed.  Keep it
            # orange until there is either a durable success or no attempt
            # was ever submitted at all.
            "failed": 2,
            "pending": 3,
            "delivered": 4,
        }

        def set_kind(edge: dict[str, Any], kind: str, label: str) -> None:
            current = str(edge.get("kind", "observed"))
            if priority.get(kind, 0) >= priority.get(current, 0):
                edge["kind"] = kind
                edge["label"] = label

        def add_reported_edge(reporter: str, detail: str, observed_at_ms: int) -> None:
            # Query-call replies commonly say e.g. "heard DG3YDE at -8 dB".
            # This is evidence about a remote link, not proof that our own
            # transmission reached the reporter.
            match = re.search(
                r"heard\s+([A-Z0-9/]+)\s+at\s+([+-]?\d+(?:\.\d+)?)\s*dB",
                detail,
                re.IGNORECASE,
            )
            reported_target = match.group(1).upper() if match else destination
            try:
                reported_snr = float(match.group(2)) if match else None
            except (TypeError, ValueError):
                reported_snr = None
            if (
                reporter.startswith("@")
                or reported_target.startswith("@")
                or reporter == reported_target
            ):
                return
            nodes.update((reporter, reported_target))
            edge = edge_for(reporter, reported_target)
            edge["latest"] = max(int(edge["latest"]), observed_at_ms)
            edge["reported_by"] = reporter
            edge["reported_target"] = reported_target
            if reported_snr is not None:
                edge["snr"] = reported_snr
            label = "reported"
            if reported_snr is not None:
                label = f"reported {reported_snr:g} dB"
            set_kind(edge, "reported", label)

        evidence_cutoff_ms = now - MESSAGE_GRAPH_EVIDENCE_MAX_AGE_MS
        for observation in self.database.recent_observations(500, band=band):
            if int(observation["observed_at_ms"]) < evidence_cutoff_ms:
                continue
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
            edge = edge_for(source, target)
            edge["count"] = int(edge["count"]) + 1
            edge["latest"] = max(int(edge["latest"]), int(observation["observed_at_ms"]))
            if str(params.get("EVIDENCE", "")).startswith("remote_web_"):
                edge["network_hint"] = True
            else:
                edge["rf_observed"] = True
            snr = params.get("SNR")
            if isinstance(snr, (int, float)):
                edge["snr"] = snr if edge["snr"] is None else max(float(edge["snr"]), float(snr))

        attempts = self.database.list_attempts(message_id)
        transactions = self.database.list_transmission_transactions(message_id)
        paths: list[dict[str, Any]] = []

        for transaction in transactions:
            try:
                raw_path = json.loads(str(transaction.get("path_json") or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                raw_path = []
            path = tuple(str(item).strip().upper() for item in raw_path if str(item).strip())
            if len(path) < 2:
                continue
            nodes.update(path)
            tx_status = str(transaction.get("status") or "")
            operation = str(transaction.get("operation") or "")
            if tx_status == "acknowledged":
                kind, label = "delivered", f"{operation} delivered"
            elif tx_status in {"timed_out", "unconfirmed"} and message_state in {
                "failed",
                "expired",
            }:
                kind, label = "failed", f"{operation} failed"
            elif tx_status in {
                "timed_out",
                "unconfirmed",
                "queued",
                "tx_active",
                "awaiting_ack",
            }:
                kind, label = "pending", f"{operation} pending"
            else:
                kind, label = "failed", f"{operation} failed"
            first_hop = (path[0], path[1])
            edge = edge_for(*first_hop)
            edge["attempts"] = int(edge.get("attempts", 0)) + 1
            edge["last_action"] = operation
            edge["last_status"] = tx_status
            edge["attempt_number"] = max(
                int(edge.get("attempt_number") or 0),
                int(transaction.get("retry_number") or 0) + 1,
            )
            edge["path"] = list(path)
            set_kind(edge, kind, label)
            paths.append(
                {
                    "path": list(path),
                    "operation": operation,
                    "status": tx_status,
                    "attempt": int(transaction.get("retry_number") or 0) + 1,
                    "kind": kind,
                }
            )

        operation_attempt_numbers: dict[tuple[str, str], int] = {}
        for attempt_number, attempt in enumerate(attempts, start=1):
            action = str(attempt["action"]).lower()
            target = str(attempt["target"]).upper()
            detail = str(attempt.get("detail") or "")
            status = str(attempt["status"]).lower()
            created_at_ms = int(attempt["created_at_ms"])
            if action == "route_evidence" and status in {"received", "available"}:
                add_reported_edge(target, detail, created_at_ms)
                continue
            if (
                target.startswith("@")
                or target == "ROUTE"
                or target == origin
                or action
                in {
                    "defer",
                    "route",
                    "route_evidence_settling",
                    "snr_probe",
                    "hearing_probe",
                    "hearing_query",
                    "allcall_query_call",
                    "candidate_query_call",
                    "capability",
                    "capability_wait",
                    "capability_timeout",
                }
            ):
                continue
            nodes.add(target)
            edge = edge_for(origin, target)
            edge["attempts"] = int(edge.get("attempts", 0)) + 1
            edge["last_action"] = action
            edge["last_status"] = status
            operation_key = (origin, target)
            operation_attempt_numbers[operation_key] = (
                operation_attempt_numbers.get(operation_key, 0) + 1
            )
            # Transaction retry numbers are more meaningful than the global
            # audit-row index.  Do not replace one with a large discovery-log
            # row number when a durable transaction already supplied it.
            if not edge.get("attempt_number"):
                edge["attempt_number"] = operation_attempt_numbers[operation_key]
            if action in {"delivery_ack", "standard_ack", "hop_ack", "custody_ack"} and status in {
                "received",
                "confirmed",
                "available",
            }:
                set_kind(edge, "delivered", f"{action.replace('_', ' ')}")
            elif status in {"failed", "error"}:
                set_kind(edge, "failed", f"{action} failed")
            elif status in {"started", "submitted", "uncertain", "deferred", "waiting"}:
                set_kind(edge, "pending", f"{action} pending")
            elif status in {"received", "confirmed", "available"}:
                set_kind(edge, "delivered", f"{action} delivered")

        # Also expose paths recorded before a transaction was created.  This
        # is useful after upgrades and for historical route selections.
        known_paths = {tuple(item["path"]) for item in paths}
        for path in self.database.message_paths(message_id):
            normalized = tuple(str(item).upper() for item in path)
            if len(normalized) >= 2 and normalized not in known_paths:
                nodes.update(normalized)
                paths.append(
                    {
                        "path": list(normalized),
                        "operation": "route",
                        "status": "selected",
                        "attempt": None,
                        "kind": "reported",
                    }
                )

        # Do not let a long-lived mailbox turn a graph request into a dump of
        # every historical retry.  The database remains complete; the graph
        # only needs the most recent route attempts for interpretation.
        paths = paths[-MESSAGE_GRAPH_MAX_PATHS:]

        # Keep the durable attempt/audit history untouched, but bound the
        # graph itself. Passive RF/network evidence is time-limited above;
        # operation edges are retained when they belong to a recent path and
        # otherwise capped to the newest attempted links. This prevents a
        # multi-day undelivered message from turning the visual graph into a
        # dump of every historical custodian considered.
        recent_path_pairs = {
            (path[0], path[1])
            for item in paths
            for path in [tuple(str(node).upper() for node in item.get("path", []))]
            if len(path) >= 2
        }
        operation_edges = [edge for edge in edges.values() if int(edge.get("attempts", 0) or 0) > 0]
        if len(operation_edges) > MESSAGE_GRAPH_MAX_OPERATION_EDGES:
            keep_operation_keys = set(recent_path_pairs)
            keep_operation_keys.update(
                (str(edge["from"]), str(edge["to"]))
                for edge in sorted(
                    operation_edges,
                    key=lambda item: int(item.get("latest", 0) or 0),
                    reverse=True,
                )[:MESSAGE_GRAPH_MAX_OPERATION_EDGES]
            )
            edges = {
                key: edge
                for key, edge in edges.items()
                if int(edge.get("attempts", 0) or 0) == 0 or key in keep_operation_keys
            }
        nodes = {origin, destination}
        for edge in edges.values():
            nodes.update((str(edge["from"]), str(edge["to"])))
        for item in paths:
            nodes.update(str(node).upper() for node in item.get("path", []))
        return {
            "message_id": message_id,
            "origin": origin,
            "destination": destination,
            "nodes": sorted(nodes),
            "edges": list(edges.values()),
            "paths": paths,
        }

        return {
            "message_id": message_id,
            "origin": origin,
            "destination": destination,
            "nodes": sorted(nodes),
            "edges": list(edges.values()),
        }

    def live_activity_graph(
        self,
        band: str,
        *,
        local_callsign: str | None = None,
        max_age_ms: int = 2 * 60 * 60 * 1000,
    ) -> dict[str, object]:
        """Return a compact graph classified by recent reciprocal evidence.

        RX observations describe traffic heard from other stations, but the
        API's TX events intentionally contain tones rather than the original
        text.  Include durable submitted TX transactions as local-direction
        evidence too; otherwise a perfectly successful local exchange is
        incorrectly rendered as one-way forever.
        """
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
            if not isinstance(source, str):
                # RX.SPOT uses CALL, while display-oriented heartbeat/activity
                # records often carry ``CALLSIGN: ...`` only in value.
                source = params.get("CALL")
            if not isinstance(source, str):
                source_match = re.match(
                    r"^\s*([A-Z0-9/]{1,16})\s*:",
                    str(observation.get("value", "")),
                    re.IGNORECASE,
                )
                source = source_match.group(1) if source_match is not None else None
            if not isinstance(source, str):
                continue
            source = source.strip().upper()
            if not source or source.startswith("@"):
                continue
            # JS8Call commonly exposes a received heartbeat, CQ, or SNR
            # response with FROM but no TO.  Recently heard already treats
            # that as direct RF evidence.  Project it as a one-way edge to
            # this station as well, otherwise the live graph silently omits
            # exactly the traffic the operator just heard.
            if isinstance(destination, str):
                destination = destination.strip().upper()
            else:
                destination = (local_callsign or "").strip().upper()
            if not destination or destination.startswith("@") or source == destination:
                continue
            nodes.update((source, destination))
            key = (source, destination)
            item = directions.setdefault(
                key, {"latest": 0, "count": 0, "active": 0, "js8m": False, "snr": None}
            )
            item["latest"] = max(int(item["latest"]), int(observation["observed_at_ms"]))
            item["count"] = int(item["count"]) + 1
            if age_ms <= 10 * 60 * 1000:
                item["active"] = int(item["active"]) + 1
            text = f"{observation['value']} {params.get('TEXT', '')}".upper()
            item["js8m"] = bool(item["js8m"] or "J8M" in text or "JS8MAIL" in text)
            snr = params.get("SNR")
            if isinstance(snr, (int, float)):
                item["snr"] = snr if item["snr"] is None else max(float(item["snr"]), float(snr))

        # Add local RF transmissions.  A transaction is only evidence after
        # it was handed to JS8Call; a route selected by the planner alone must
        # not appear as live radio activity.  The path is durable so this also
        # survives the short API/UI refresh gap around a TX cycle.
        for transaction in self.database.recent_transmission_transactions(
            now - max_age_ms, band=band
        ):
            try:
                path = tuple(
                    str(item).strip().upper() for item in json.loads(transaction["path_json"])
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            path = tuple(item for item in path if item and not item.startswith("@"))
            if len(path) < 2:
                continue
            latest = int(
                transaction.get("tx_finished_at_ms")
                or transaction.get("submitted_at_ms")
                or transaction.get("created_at_ms")
                or 0
            )
            age_ms = max(0, now - latest)
            if age_ms > max_age_ms:
                continue
            for source, destination in pairwise(path):
                if source == destination:
                    continue
                nodes.update((source, destination))
                item = directions.setdefault(
                    (source, destination),
                    {"latest": 0, "count": 0, "active": 0, "js8m": False, "snr": None},
                )
                item["latest"] = max(int(item["latest"]), latest)
                item["count"] = int(item["count"]) + 1
                if age_ms <= 10 * 60 * 1000:
                    item["active"] = int(item["active"]) + 1

        # Outcome records are another durable form of local TX evidence. They
        # are written when an ACK/timeout settles a transaction and can be the
        # only surviving direction after a restart.
        for link in self.database.temporal_link_views(5000):
            if str(link.get("band", "")).lower() != band.strip().lower():
                continue
            source = str(link.get("source", "")).strip().upper()
            destination = str(link.get("destination", "")).strip().upper()
            if (
                not source
                or not destination
                or source == destination
                or source.startswith("@")
                or destination.startswith("@")
            ):
                continue
            latest = int(link.get("last_observed_at_ms") or 0)
            age_ms = max(0, now - latest)
            if age_ms > max_age_ms:
                continue
            nodes.update((source, destination))
            item = directions.setdefault(
                (source, destination),
                {"latest": 0, "count": 0, "active": 0, "js8m": False, "snr": None},
            )
            item["latest"] = max(int(item["latest"]), latest)
            item["count"] = max(int(item["count"]), int(link.get("observation_count") or 0))
            if age_ms <= 10 * 60 * 1000:
                item["active"] = max(int(item["active"]), 1)
            item["js8m"] = bool(item["js8m"] or int(link.get("js8m_observation_count") or 0) > 0)
            link_snr = link.get("max_snr")
            if isinstance(link_snr, (int, float)):
                item["snr"] = (
                    link_snr if item["snr"] is None else max(float(item["snr"]), float(link_snr))
                )
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
            reciprocal = bool(
                forward
                and reverse
                and min(int(forward["latest"]), int(reverse["latest"])) >= now - 10 * 60 * 1000
            )
            active_count = max(int(forward.get("active", 0)), int(reverse.get("active", 0)))
            kind = (
                "reciprocal"
                if reciprocal
                else ("active_one_way" if active_count >= 2 else "isolated_one_way")
            )
            js8m = bool(forward.get("js8m", False) or reverse.get("js8m", False))
            edges.append(
                {
                    "from": left,
                    "to": right,
                    "kind": kind,
                    "age_seconds": age_ms // 1000,
                    "freshness": round(freshness, 3),
                    "snr": forward.get("snr")
                    if forward.get("snr") is not None
                    else reverse.get("snr"),
                    "observations": sum(int(directions[key]["count"]) for key in directed_keys),
                    "js8m": js8m,
                }
            )
        return {"band": band, "generated_at_ms": now, "nodes": sorted(nodes), "edges": edges}

    def station_views(
        self,
        limit: int = 30,
        band: str | None = None,
        exclude_callsign: str | None = None,
    ) -> list[dict[str, object]]:
        """Summarise recent direct and remotely reported callsigns."""
        now = utc_now_ms()
        stations: dict[str, dict[str, Any]] = {}
        excluded = exclude_callsign.strip().upper() if exclude_callsign else ""
        for observation in self.database.recent_observations(500, band=band):
            params = observation["params"]
            source = params.get("FROM")
            target = params.get("TO")
            snr = params.get("SNR")
            observed = int(observation["observed_at_ms"])
            if isinstance(source, str) and source and not source.startswith("@"):
                call = source.upper()
                if call == excluded:
                    source = None
                else:
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
                        item["snr"] = (
                            snr if item["snr"] is None else max(float(item["snr"]), float(snr))
                        )
                    evidence = item["evidence"]
                    assert isinstance(evidence, set)
                    evidence.add(
                        "direct"
                        if observation["event_type"] != "QUERY.CALL.RESPONSE"
                        else "remote_report"
                    )
            if isinstance(target, str) and target and not target.startswith("@"):
                call = target.upper()
                if call != excluded:
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
                        item["snr"] = (
                            snr if item["snr"] is None else max(float(item["snr"]), float(snr))
                        )
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
        self,
        callsign: str,
        now_ms: int | None = None,
        window_ms: int = 600_000,
        band: str | None = None,
    ) -> bool:
        return self.recent_heard_age_ms(callsign, now_ms, window_ms, band) is not None

    def is_js8m_capable(self, callsign: str) -> bool:
        """Return whether a peer has a currently valid JS8Mail capability record."""
        return self.database.peer_capabilities(callsign.strip()) is not None

    def recent_heard_age_ms(
        self,
        callsign: str,
        now_ms: int | None = None,
        window_ms: int = 600_000,
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
            params = observation["params"]
            source = params.get("FROM") or params.get("CALL")
            if not isinstance(source, str):
                # Some JS8Call builds expose the first decoded directed frame
                # only as RX.ACTIVITY text, e.g. ``M0OUE: OH3SPN MSG``.
                match = re.match(
                    r"^\s*([A-Z0-9/]{1,16})\s*:",
                    str(observation["value"]),
                    re.IGNORECASE,
                )
                source = match.group(1) if match else None
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
        return (
            self.recent_answered_age_ms(callsign, local_callsign, now_ms, window_ms, band)
            is not None
        )

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
        self,
        destination: str,
        now_ms: int | None = None,
        window_ms: int = 600_000,
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
