"""Minimal durable SQLite store for the first vertical slice."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from js8mail.domain import NormalizedEvent, utc_now_ms

SCHEMA_VERSION = 7


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        # The local UI serves requests in worker threads while the daemon
        # records radio events. SQLite's serialized connection mode plus the
        # application-level small operations make this safe for this slice.
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def _migrate(self) -> None:
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at_ms INTEGER NOT NULL)"
        )
        current = self.connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
        if current < 1:
            self.connection.executescript(
                """
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY,
                event_type TEXT NOT NULL,
                correlation_id TEXT,
                payload_json TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY,
                event_type TEXT NOT NULL,
                value TEXT NOT NULL,
                params_json TEXT NOT NULL,
                observed_at_ms INTEGER NOT NULL,
                received_at_ms INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS observations_observed_at_idx
                ON observations(observed_at_ms);
            INSERT INTO schema_migrations(version, applied_at_ms) VALUES (1, strftime('%s','now') * 1000);
            """
            )
            current = 1
        if current < 2:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    destination TEXT NOT NULL,
                    subject TEXT NOT NULL DEFAULT '',
                    body TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0 CHECK(priority BETWEEN 0 AND 3),
                    state TEXT NOT NULL,
                    expires_at_ms INTEGER,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_state_idx ON messages(state, updated_at_ms);
                INSERT INTO schema_migrations(version, applied_at_ms) VALUES (2, strftime('%s','now') * 1000);
                """
            )
        if current < 3:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS message_attempts (
                    id INTEGER PRIMARY KEY,
                    message_id TEXT NOT NULL REFERENCES messages(id),
                    action TEXT NOT NULL,
                    target TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS message_attempts_message_idx
                    ON message_attempts(message_id, created_at_ms);
                INSERT INTO schema_migrations(version, applied_at_ms) VALUES (3, strftime('%s','now') * 1000);
                """
            )
        if current < 4:
            self.connection.executescript(
                """
                ALTER TABLE messages ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE messages ADD COLUMN next_attempt_at_ms INTEGER;
                INSERT INTO schema_migrations(version, applied_at_ms) VALUES (4, strftime('%s','now') * 1000);
                """
            )
        if current < 5:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS message_parts (
                    message_id TEXT NOT NULL,
                    part_number INTEGER NOT NULL CHECK(part_number > 0),
                    total_parts INTEGER NOT NULL CHECK(total_parts > 0),
                    payload TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK(direction IN ('outgoing', 'incoming')),
                    peer TEXT NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    PRIMARY KEY(message_id, part_number, direction, peer)
                );
                CREATE INDEX IF NOT EXISTS message_parts_lookup_idx
                    ON message_parts(message_id, direction, peer);
                INSERT INTO schema_migrations(version, applied_at_ms) VALUES (5, strftime('%s','now') * 1000);
                """
            )
        if current < 6:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS peer_capabilities (
                    peer TEXT PRIMARY KEY,
                    protocol_version INTEGER NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    observed_at_ms INTEGER NOT NULL,
                    expires_at_ms INTEGER NOT NULL
                );
                INSERT INTO schema_migrations(version, applied_at_ms) VALUES (6, strftime('%s','now') * 1000);
                """
            )
        if current < 7:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS custody (
                    message_id TEXT NOT NULL,
                    custodian TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('offered', 'accepted', 'retrieval_pending', 'forwarded', 'failed')),
                    updated_at_ms INTEGER NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(message_id, custodian)
                );
                CREATE INDEX IF NOT EXISTS custody_status_idx ON custody(status, updated_at_ms);
                INSERT INTO schema_migrations(version, applied_at_ms) VALUES (7, strftime('%s','now') * 1000);
                """
            )
        self.connection.commit()

    def record_observation(self, event: NormalizedEvent) -> int:
        cursor = self.connection.execute(
            "INSERT INTO observations(event_type, value, params_json, observed_at_ms, received_at_ms) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                event.event_type,
                event.value,
                json.dumps(event.params, sort_keys=True),
                event.received_at_ms,
                utc_now_ms(),
            ),
        )
        self.connection.commit()
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return an observation row id")
        return int(cursor.lastrowid)

    def audit(
        self, event_type: str, payload: dict[str, Any], correlation_id: str | None = None
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO audit_events(event_type, correlation_id, payload_json, created_at_ms) VALUES (?, ?, ?, ?)",
            (event_type, correlation_id, json.dumps(payload, sort_keys=True), utc_now_ms()),
        )
        self.connection.commit()
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return an audit row id")
        return int(cursor.lastrowid)

    def latest_audit_time(
        self, event_type: str, payload_key: str | None = None, payload_value: str | None = None
    ) -> int | None:
        rows = self.connection.execute(
            "SELECT payload_json, created_at_ms FROM audit_events WHERE event_type = ? "
            "ORDER BY created_at_ms DESC LIMIT 100",
            (event_type,),
        ).fetchall()
        for row in rows:
            if payload_key is not None:
                payload = json.loads(row["payload_json"])
                if payload.get(payload_key) != payload_value:
                    continue
            return int(row["created_at_ms"])
        return None

    def enqueue_message(
        self,
        message_id: str,
        destination: str,
        body: str,
        *,
        subject: str = "",
        priority: int = 0,
        expires_at_ms: int | None = None,
    ) -> None:
        now = utc_now_ms()
        self.connection.execute(
            "INSERT INTO messages(id, destination, subject, body, priority, state, expires_at_ms, created_at_ms, updated_at_ms) "
            "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
            (message_id, destination, subject, body, priority, expires_at_ms, now, now),
        )
        self.connection.commit()
        self.audit("message.queued", {"message_id": message_id, "destination": destination})

    def transition_message(self, message_id: str, target_state: str) -> None:
        from js8mail.application.lifecycle import MessageState, can_transition

        row = self.connection.execute(
            "SELECT state FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        if row is None:
            raise KeyError(message_id)
        current = MessageState(row[0])
        target = MessageState(target_state)
        if not can_transition(current, target):
            raise ValueError(f"Invalid message transition: {current} -> {target}")
        now = utc_now_ms()
        self.connection.execute(
            "UPDATE messages SET state = ?, updated_at_ms = ? WHERE id = ?",
            (target, now, message_id),
        )
        self.connection.execute(
            "INSERT INTO audit_events(event_type, correlation_id, payload_json, created_at_ms) VALUES (?, ?, ?, ?)",
            ("message.state_changed", message_id, json.dumps({"from": current, "to": target}), now),
        )
        self.connection.commit()

    def list_messages(self, state: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM messages"
        parameters: tuple[str, ...] = ()
        if state is not None:
            query += " WHERE state = ?"
            parameters = (state,)
        query += " ORDER BY created_at_ms DESC"
        return [dict(row) for row in self.connection.execute(query, parameters).fetchall()]

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def recent_observations(self, limit: int = 50) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(limit, 500))
        rows = self.connection.execute(
            "SELECT id, event_type, value, params_json, observed_at_ms "
            "FROM observations ORDER BY observed_at_ms DESC LIMIT ?",
            (bounded_limit,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["params"] = json.loads(item.pop("params_json"))
            result.append(item)
        return result

    def message_counts(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT state, COUNT(*) AS count FROM messages GROUP BY state"
        ).fetchall()
        return {str(row["state"]): int(row["count"]) for row in rows}

    def record_attempt(
        self, message_id: str, action: str, target: str, status: str, detail: str = ""
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO message_attempts(message_id, action, target, status, detail, created_at_ms) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, action, target, status, detail[:500], utc_now_ms()),
        )
        self.connection.commit()
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return an attempt row id")
        return int(cursor.lastrowid)

    def list_attempts(self, message_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM message_attempts WHERE message_id = ? ORDER BY created_at_ms",
            (message_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def upsert_message_part(
        self,
        message_id: str,
        part_number: int,
        total_parts: int,
        payload: str,
        *,
        direction: str,
        peer: str,
    ) -> None:
        if direction not in {"outgoing", "incoming"}:
            raise ValueError("invalid message part direction")
        if not 1 <= part_number <= total_parts or total_parts > 255:
            raise ValueError("invalid message part position")
        if len(payload.encode()) > 4096 or len(peer) > 16:
            raise ValueError("message part is too large")
        self.connection.execute(
            "INSERT INTO message_parts(message_id, part_number, total_parts, payload, direction, peer, updated_at_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(message_id, part_number, direction, peer) DO UPDATE SET "
            "total_parts=excluded.total_parts, payload=excluded.payload, updated_at_ms=excluded.updated_at_ms",
            (message_id, part_number, total_parts, payload, direction, peer.upper(), utc_now_ms()),
        )
        self.connection.commit()

    def list_message_parts(
        self, message_id: str, *, direction: str, peer: str
    ) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM message_parts WHERE message_id = ? AND direction = ? AND peer = ? "
            "ORDER BY part_number",
            (message_id, direction, peer.upper()),
        ).fetchall()
        return [dict(row) for row in rows]

    def upsert_peer_capabilities(
        self, peer: str, protocol_version: int, capabilities: tuple[str, ...], expires_at_ms: int
    ) -> None:
        self.connection.execute(
            "INSERT INTO peer_capabilities(peer, protocol_version, capabilities_json, observed_at_ms, expires_at_ms) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(peer) DO UPDATE SET protocol_version=excluded.protocol_version, "
            "capabilities_json=excluded.capabilities_json, observed_at_ms=excluded.observed_at_ms, "
            "expires_at_ms=excluded.expires_at_ms",
            (peer.upper(), protocol_version, json.dumps(capabilities), utc_now_ms(), expires_at_ms),
        )
        self.connection.commit()

    def peer_capabilities(self, peer: str) -> tuple[int, tuple[str, ...]] | None:
        row = self.connection.execute(
            "SELECT protocol_version, capabilities_json, expires_at_ms FROM peer_capabilities WHERE peer = ?",
            (peer.upper(),),
        ).fetchone()
        if row is None or int(row["expires_at_ms"]) <= utc_now_ms():
            return None
        return int(row["protocol_version"]), tuple(json.loads(row["capabilities_json"]))

    def upsert_custody(self, message_id: str, custodian: str, status: str, detail: str = "") -> None:
        if status not in {"offered", "accepted", "retrieval_pending", "forwarded", "failed"}:
            raise ValueError("invalid custody status")
        self.connection.execute(
            "INSERT INTO custody(message_id, custodian, status, updated_at_ms, detail) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(message_id, custodian) DO UPDATE SET status=excluded.status, "
            "updated_at_ms=excluded.updated_at_ms, detail=excluded.detail",
            (message_id, custodian.upper(), status, utc_now_ms(), detail[:500]),
        )
        self.connection.commit()

    def list_custody(self, message_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM custody WHERE message_id = ? ORDER BY updated_at_ms", (message_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def defer_message(self, message_id: str, delay_ms: int, detail: str) -> None:
        from js8mail.application.lifecycle import MessageState, can_transition

        row = self.connection.execute("SELECT state, retry_count FROM messages WHERE id = ?", (message_id,)).fetchone()
        if row is None:
            raise KeyError(message_id)
        current = MessageState(row["state"])
        if current != MessageState.WAITING_ROUTE and not can_transition(current, MessageState.WAITING_ROUTE):
            raise ValueError(f"Cannot defer message in state {current}")
        now = utc_now_ms()
        self.connection.execute(
            "UPDATE messages SET state = ?, retry_count = ?, next_attempt_at_ms = ?, updated_at_ms = ? WHERE id = ?",
            (MessageState.WAITING_ROUTE, int(row["retry_count"]) + 1, now + max(1000, delay_ms), now, message_id),
        )
        self.connection.commit()
        self.record_attempt(message_id, "defer", "route", "waiting", detail)

    def due_for_retry(self, message_id: str) -> bool:
        row = self.connection.execute(
            "SELECT next_attempt_at_ms FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        return row is not None and (row["next_attempt_at_ms"] is None or row["next_attempt_at_ms"] <= utc_now_ms())

    def delete_message(self, message_id: str) -> None:
        row = self.connection.execute("SELECT state FROM messages WHERE id = ?", (message_id,)).fetchone()
        if row is None:
            raise KeyError(message_id)
        if row["state"] == "in_progress":
            raise ValueError("cannot remove a message currently in progress")
        self.connection.execute("DELETE FROM message_attempts WHERE message_id = ?", (message_id,))
        self.connection.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        self.connection.commit()
        self.audit("message.removed", {"message_id": message_id})
