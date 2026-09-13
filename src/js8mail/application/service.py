"""Mailbox operations used by the local UI."""

from __future__ import annotations

import secrets

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

    def plan_route(self, origin: str, destination: str, now_ms: int | None = None) -> RoutePlan:
        now = utc_now_ms() if now_ms is None else now_ms
        graph = TemporalGraph()
        for observation in self.database.recent_observations(500):
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
            graph.add(
                LinkEvidence(
                    source,
                    target,
                    observation["observed_at_ms"],
                    score,
                    expected_airtime_ms=1000,
                )
            )
        return RouteEngine(graph).choose(origin, destination, now_ms=now)

    def message_views(self) -> list[dict[str, object]]:
        views: list[dict[str, object]] = []
        for message in self.database.list_messages():
            view = dict(message)
            view["attempts"] = self.database.list_attempts(str(message["id"]))
            attempts = view["attempts"]
            if any(
                attempt["action"] == "delivery_ack" and attempt["status"] == "received"
                for attempt in attempts
            ):
                view["confidence"] = "delivered_to_js8mail"
            elif any(
                attempt["action"] in {"hop_ack", "standard_ack"}
                and attempt["status"] == "received"
                for attempt in attempts
            ):
                view["confidence"] = "radio_acknowledged"
            elif any(
                attempt["status"] == "submitted" for attempt in attempts
            ):
                view["confidence"] = "submitted_to_js8call"
            else:
                view["confidence"] = "uncertain"
            views.append(view)
        return views

    def recently_heard(
        self, callsign: str, now_ms: int | None = None, window_ms: int = 600_000
    ) -> bool:
        now = utc_now_ms() if now_ms is None else now_ms
        wanted = callsign.strip().upper()
        for observation in self.database.recent_observations(500):
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
    ) -> bool:
        """Return true only for a recent directed response to this station."""
        now = utc_now_ms() if now_ms is None else now_ms
        wanted = callsign.strip().upper()
        local = local_callsign.strip().upper()
        if not wanted or not local:
            return False
        for observation in self.database.recent_observations(500):
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
        self, destination: str, now_ms: int | None = None, window_ms: int = 600_000
    ) -> list[str]:
        wanted = destination.strip().upper()
        now = utc_now_ms() if now_ms is None else now_ms
        scores: dict[str, float] = {}
        for observation in self.database.recent_observations(500):
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
            station for station, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        ]
