"""Minimal durable SQLite store for the first vertical slice."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from js8mail.domain import NormalizedEvent, utc_now_ms

SCHEMA_VERSION = 2


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path)
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
