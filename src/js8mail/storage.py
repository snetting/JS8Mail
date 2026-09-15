"""Minimal durable SQLite store for the first vertical slice."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from js8mail.bands import context_from_params
from js8mail.domain import NormalizedEvent, utc_now_ms

SCHEMA_VERSION = 21


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        # The local UI uses worker threads while the daemon consumes the radio
        # socket. Keep one SQLite connection per thread; WAL then gives us
        # safe concurrent readers without sharing Python transaction state.
        self._thread_local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._migrate()

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        with self._connections_lock:
            self._connections.append(connection)
        return connection

    @property
    def connection(self) -> sqlite3.Connection:
        connection = getattr(self._thread_local, "connection", None)
        if connection is None:
            connection = self._open_connection()
            self._thread_local.connection = connection
        return connection

    def close_thread_connection(self) -> None:
        """Close and forget the connection owned by the current thread."""
        connection = getattr(self._thread_local, "connection", None)
        if connection is None:
            return
        self._thread_local.connection = None
        with self._connections_lock:
            try:
                self._connections.remove(connection)
            except ValueError:
                pass
        connection.close()

    def close(self) -> None:
        with self._connections_lock:
            connections, self._connections = self._connections, []
        for connection in connections:
            connection.close()

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
        if current < 8:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS inbox_messages (
                    sender TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    body TEXT NOT NULL,
                    total_parts INTEGER NOT NULL CHECK(total_parts > 0),
                    received_parts_json TEXT NOT NULL,
                    complete INTEGER NOT NULL CHECK(complete IN (0, 1)),
                    path TEXT NOT NULL DEFAULT '',
                    first_received_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    PRIMARY KEY(sender, message_id)
                );
                CREATE INDEX IF NOT EXISTS inbox_messages_updated_idx
                    ON inbox_messages(updated_at_ms DESC);
                INSERT INTO schema_migrations(version, applied_at_ms) VALUES (8, strftime('%s','now') * 1000);
                """
            )
        if current < 9:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS groups (
                    name TEXT PRIMARY KEY,
                    description TEXT NOT NULL DEFAULT '',
                    first_seen_at_ms INTEGER NOT NULL,
                    last_seen_at_ms INTEGER NOT NULL,
                    seen_count INTEGER NOT NULL DEFAULT 0,
                    subscribed INTEGER NOT NULL DEFAULT 0 CHECK(subscribed IN (0, 1)),
                    auto_forward INTEGER NOT NULL DEFAULT 0 CHECK(auto_forward IN (0, 1))
                );
                INSERT INTO schema_migrations(version, applied_at_ms) VALUES (9, strftime('%s','now') * 1000);
                """
            )
        if current < 10:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS station_sessions (
                    station TEXT NOT NULL,
                    session_bucket TEXT NOT NULL,
                    first_seen_at_ms INTEGER NOT NULL,
                    last_seen_at_ms INTEGER NOT NULL,
                    observation_count INTEGER NOT NULL DEFAULT 0,
                    band TEXT NOT NULL DEFAULT '',
                    speed TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(station, session_bucket)
                );
                CREATE TABLE IF NOT EXISTS temporal_links (
                    source TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    band TEXT NOT NULL DEFAULT '',
                    speed TEXT NOT NULL DEFAULT '',
                    first_observed_at_ms INTEGER NOT NULL,
                    last_observed_at_ms INTEGER NOT NULL,
                    observation_count INTEGER NOT NULL DEFAULT 0,
                    max_snr REAL,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(source, destination, band, speed)
                );
                CREATE INDEX IF NOT EXISTS temporal_links_recent_idx
                    ON temporal_links(last_observed_at_ms DESC);
                INSERT INTO schema_migrations(version, applied_at_ms) VALUES (10, strftime('%s','now') * 1000);
                """
            )
        if current < 11:
            self.connection.executescript(
                """
                ALTER TABLE inbox_messages ADD COLUMN group_name TEXT NOT NULL DEFAULT '';
                CREATE INDEX IF NOT EXISTS inbox_messages_group_idx
                    ON inbox_messages(group_name, updated_at_ms DESC);
                INSERT INTO schema_migrations(version, applied_at_ms) VALUES (11, strftime('%s','now') * 1000);
                """
            )
        if current < 12:
            self.connection.executescript(
                """
                ALTER TABLE temporal_links ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0;
                CREATE TABLE IF NOT EXISTS airtime_usage (
                    scope TEXT PRIMARY KEY,
                    window_started_at_ms INTEGER,
                    window_used_ms INTEGER NOT NULL DEFAULT 0,
                    message_used_ms INTEGER NOT NULL DEFAULT 0,
                    updated_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS message_airtime (
                    message_id TEXT PRIMARY KEY REFERENCES messages(id),
                    used_ms INTEGER NOT NULL DEFAULT 0,
                    updated_at_ms INTEGER NOT NULL
                );
                INSERT INTO schema_migrations(version, applied_at_ms) VALUES (12, strftime('%s','now') * 1000);
                """
            )
        if current < 13:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS message_paths (
                    message_id TEXT NOT NULL REFERENCES messages(id),
                    path TEXT NOT NULL,
                    first_attempted_at_ms INTEGER NOT NULL,
                    last_attempted_at_ms INTEGER NOT NULL,
                    PRIMARY KEY(message_id, path)
                );
                CREATE INDEX IF NOT EXISTS message_paths_message_idx
                    ON message_paths(message_id, last_attempted_at_ms DESC);
                INSERT INTO schema_migrations(version, applied_at_ms) VALUES (13, strftime('%s','now') * 1000);
                """
            )
        if current < 14:
            self.connection.executescript(
                """
                ALTER TABLE observations ADD COLUMN band TEXT NOT NULL DEFAULT '';
                ALTER TABLE observations ADD COLUMN dial_frequency INTEGER;
                CREATE INDEX IF NOT EXISTS observations_band_time_idx
                    ON observations(band, observed_at_ms DESC);
                INSERT INTO schema_migrations(version, applied_at_ms) VALUES (14, strftime('%s','now') * 1000);
                """
            )
            # Recover normalized bands for observations created before the
            # band-aware schema. Their original API parameters retain DIAL or
            # FREQ, so this does not guess from UI state.
            legacy_rows = self.connection.execute(
                "SELECT id, params_json FROM observations WHERE band = ''"
            ).fetchall()
            for row in legacy_rows:
                try:
                    params = json.loads(str(row["params_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                band, dial_frequency = context_from_params(params)
                if band or dial_frequency is not None:
                    self.connection.execute(
                        "UPDATE observations SET band = ?, dial_frequency = ? WHERE id = ?",
                        (band, dial_frequency, row["id"]),
                    )
        if current < 15:
            # Also repair databases that already applied migration 14 before
            # the legacy backfill was added.
            legacy_rows = self.connection.execute(
                "SELECT id, params_json FROM observations WHERE band = ''"
            ).fetchall()
            for row in legacy_rows:
                try:
                    params = json.loads(str(row["params_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                band, dial_frequency = context_from_params(params)
                if band or dial_frequency is not None:
                    self.connection.execute(
                        "UPDATE observations SET band = ?, dial_frequency = ? WHERE id = ?",
                        (band, dial_frequency, row["id"]),
                    )
            self.connection.execute(
                "INSERT INTO schema_migrations(version, applied_at_ms) "
                "VALUES (15, strftime('%s','now') * 1000)"
            )
        if current < 16:
            self.connection.execute(
                "ALTER TABLE temporal_links ADD COLUMN js8m_observation_count "
                "INTEGER NOT NULL DEFAULT 0"
            )
            self.connection.execute(
                "INSERT INTO schema_migrations(version, applied_at_ms) "
                "VALUES (16, strftime('%s','now') * 1000)"
            )
        if current < 17:
            # A station can be heard on more than one band in the same
            # half-hour. Keep those sessions distinct so band-filtered route
            # decisions never inherit the wrong observation context.
            self.connection.executescript(
                """
                DROP TABLE IF EXISTS station_sessions_v17;
                CREATE TABLE station_sessions_v17 (
                    station TEXT NOT NULL,
                    session_bucket TEXT NOT NULL,
                    first_seen_at_ms INTEGER NOT NULL,
                    last_seen_at_ms INTEGER NOT NULL,
                    observation_count INTEGER NOT NULL DEFAULT 0,
                    band TEXT NOT NULL DEFAULT '',
                    speed TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(station, session_bucket, band, speed)
                );
                INSERT INTO station_sessions_v17
                    (station, session_bucket, first_seen_at_ms, last_seen_at_ms,
                     observation_count, band, speed)
                    SELECT station, session_bucket, first_seen_at_ms,
                           last_seen_at_ms, observation_count, band, speed
                    FROM station_sessions;
                DROP TABLE station_sessions;
                ALTER TABLE station_sessions_v17 RENAME TO station_sessions;
                INSERT INTO schema_migrations(version, applied_at_ms)
                    VALUES (17, strftime('%s','now') * 1000);
                """
            )
        if current < 18:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS configuration (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO schema_migrations(version, applied_at_ms)
                    VALUES (18, strftime('%s','now') * 1000);
                """
            )
        if current < 19:
            self.connection.execute("ALTER TABLE messages ADD COLUMN enhanced_mode TEXT")
            self.connection.execute(
                "INSERT INTO schema_migrations(version, applied_at_ms) "
                "VALUES (19, strftime('%s','now') * 1000)"
            )
        if current < 20:
            self.connection.execute(
                "ALTER TABLE inbox_messages ADD COLUMN protocol TEXT NOT NULL DEFAULT 'standard'"
            )
            self.connection.execute(
                "INSERT INTO schema_migrations(version, applied_at_ms) "
                "VALUES (20, strftime('%s','now') * 1000)"
            )
        if current < 21:
            self.connection.execute(
                "ALTER TABLE inbox_messages ADD COLUMN delivery TEXT NOT NULL DEFAULT 'direct'"
            )
            self.connection.execute(
                "INSERT INTO schema_migrations(version, applied_at_ms) "
                "VALUES (21, strftime('%s','now') * 1000)"
            )
        self.connection.commit()

    def record_observation(
        self, event: NormalizedEvent, *, band: str = "", dial_frequency: int | None = None
    ) -> int:
        derived_band, derived_dial = context_from_params(event.params)
        band = band.strip().lower() or derived_band
        dial_frequency = dial_frequency if dial_frequency is not None else derived_dial
        cursor = self.connection.execute(
            "INSERT INTO observations(event_type, value, params_json, observed_at_ms, received_at_ms, band, dial_frequency) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_type,
                event.value,
                json.dumps(event.params, sort_keys=True),
                event.received_at_ms,
                utc_now_ms(),
                band,
                dial_frequency,
            ),
        )
        self.connection.commit()
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return an observation row id")
        return int(cursor.lastrowid)

    def record_link_projection(
        self, event: NormalizedEvent, *, band: str = "", dial_frequency: int | None = None
    ) -> None:
        source = event.params.get("FROM")
        destination = event.params.get("TO")
        if not isinstance(source, str) or not isinstance(destination, str):
            return
        if not source or not destination or source.startswith("@") or destination.startswith("@"):
            return
        observed = event.received_at_ms
        derived_band, derived_dial = context_from_params(event.params)
        band = (band.strip().lower() or derived_band)[:32]
        dial_frequency = dial_frequency if dial_frequency is not None else derived_dial
        speed = str(event.params.get("SPEED", ""))[:32]
        snr = event.params.get("SNR")
        snr_value = float(snr) if isinstance(snr, (int, float)) else None
        text = f"{event.value} {event.params.get('TEXT', '')}".upper()
        js8m_observation = int("J8M" in text or "JS8MAIL" in text)
        bucket = str(observed // (30 * 60 * 1000))
        for station in (source.upper(), destination.upper()):
            self.connection.execute(
                "INSERT INTO station_sessions(station, session_bucket, first_seen_at_ms, last_seen_at_ms, observation_count, band, speed) "
                "VALUES (?, ?, ?, ?, 1, ?, ?) ON CONFLICT(station, session_bucket, band, speed) DO UPDATE SET "
                "last_seen_at_ms=excluded.last_seen_at_ms, observation_count=station_sessions.observation_count+1",
                (station, bucket, observed, observed, band, speed),
            )
        self.connection.execute(
            "INSERT INTO temporal_links(source, destination, band, speed, first_observed_at_ms, last_observed_at_ms, observation_count, max_snr, js8m_observation_count) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?) ON CONFLICT(source, destination, band, speed) DO UPDATE SET "
            "last_observed_at_ms=excluded.last_observed_at_ms, observation_count=temporal_links.observation_count+1, "
            "max_snr=CASE WHEN excluded.max_snr IS NULL THEN temporal_links.max_snr WHEN temporal_links.max_snr IS NULL THEN excluded.max_snr ELSE MAX(temporal_links.max_snr, excluded.max_snr) END, "
            "js8m_observation_count=temporal_links.js8m_observation_count+excluded.js8m_observation_count",
            (source.upper(), destination.upper(), band, speed, observed, observed, snr_value, js8m_observation),
        )
        self.connection.commit()

    def record_link_outcome(
        self,
        source: str,
        destination: str,
        speed: int,
        snr: float | None,
        success: bool,
        band: str = "",
    ) -> None:
        """Persist per-peer speed outcomes used by adaptive policy."""
        normalized_band = band.strip().lower()
        if normalized_band:
            row = self.connection.execute(
                "SELECT band, observation_count, max_snr, success_count, failure_count FROM temporal_links "
                "WHERE source = ? AND destination = ? AND speed = ? AND band = ? "
                "ORDER BY last_observed_at_ms DESC LIMIT 1",
                (source.upper(), destination.upper(), str(speed), normalized_band),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT band, observation_count, max_snr, success_count, failure_count FROM temporal_links "
                "WHERE source = ? AND destination = ? AND speed = ? ORDER BY last_observed_at_ms DESC LIMIT 1",
                (source.upper(), destination.upper(), str(speed)),
            ).fetchone()
        now = utc_now_ms()
        normalized_band = str(row["band"]) if row else normalized_band
        self.connection.execute(
            "INSERT INTO temporal_links(source, destination, band, speed, first_observed_at_ms, last_observed_at_ms, observation_count, max_snr, success_count, failure_count) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?) ON CONFLICT(source, destination, band, speed) DO UPDATE SET "
            "last_observed_at_ms=excluded.last_observed_at_ms, observation_count=temporal_links.observation_count+1, "
            "max_snr=CASE WHEN excluded.max_snr IS NULL THEN temporal_links.max_snr WHEN temporal_links.max_snr IS NULL THEN excluded.max_snr ELSE MAX(temporal_links.max_snr, excluded.max_snr) END, "
            "success_count=temporal_links.success_count+excluded.success_count, failure_count=temporal_links.failure_count+excluded.failure_count",
            (source.upper(), destination.upper(), normalized_band, str(speed), now, now, snr, int(success), int(not success)),
        )
        self.connection.commit()

    def speed_evidence(
        self, source: str, destination: str, band: str = ""
    ) -> dict[int, dict[str, Any]]:
        if band.strip():
            rows = self.connection.execute(
                "SELECT speed, success_count, failure_count, max_snr FROM temporal_links "
                "WHERE source = ? AND destination = ? AND band = ?",
                (source.upper(), destination.upper(), band.strip().lower()),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT speed, success_count, failure_count, max_snr FROM temporal_links "
                "WHERE source = ? AND destination = ?",
                (source.upper(), destination.upper()),
            ).fetchall()
        return {
            int(row["speed"]): {
                "successes": int(row["success_count"]),
                "failures": int(row["failure_count"]),
                "average_snr": row["max_snr"],
            }
            for row in rows
            if str(row["speed"]).isdigit() and int(row["speed"]) in {0, 1, 2, 4, 8}
        }

    def airtime_state(self, scope: str = "radio") -> dict[str, int | None]:
        row = self.connection.execute("SELECT * FROM airtime_usage WHERE scope = ?", (scope,)).fetchone()
        if row is None:
            return {"window_started_at_ms": None, "window_used_ms": 0, "message_used_ms": 0}
        return {key: row[key] for key in ("window_started_at_ms", "window_used_ms", "message_used_ms")}

    def save_airtime_state(self, window_started_at_ms: int | None, window_used_ms: int, message_used_ms: int, scope: str = "radio") -> None:
        self.connection.execute(
            "INSERT INTO airtime_usage(scope, window_started_at_ms, window_used_ms, message_used_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(scope) DO UPDATE SET window_started_at_ms=excluded.window_started_at_ms, window_used_ms=excluded.window_used_ms, message_used_ms=excluded.message_used_ms, updated_at_ms=excluded.updated_at_ms",
            (scope, window_started_at_ms, window_used_ms, message_used_ms, utc_now_ms()),
        )
        self.connection.commit()

    def message_airtime_used(self, message_id: str) -> int:
        row = self.connection.execute("SELECT used_ms FROM message_airtime WHERE message_id = ?", (message_id,)).fetchone()
        return int(row[0]) if row else 0

    def save_message_airtime(self, message_id: str, used_ms: int) -> None:
        self.connection.execute(
            "INSERT INTO message_airtime(message_id, used_ms, updated_at_ms) VALUES (?, ?, ?) ON CONFLICT(message_id) DO UPDATE SET used_ms=excluded.used_ms, updated_at_ms=excluded.updated_at_ms",
            (message_id, used_ms, utc_now_ms()),
        )
        self.connection.commit()

    def temporal_link_views(self, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM temporal_links ORDER BY last_observed_at_ms DESC LIMIT ?",
            (max(1, min(limit, 5000)),),
        ).fetchall()
        return [dict(row) for row in rows]

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

    def recent_audit_events(self, event_type: str, since_ms: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload_json, created_at_ms FROM audit_events "
            "WHERE event_type = ? AND created_at_ms >= ? ORDER BY created_at_ms",
            (event_type, since_ms),
        ).fetchall()
        return [
            {"payload": json.loads(row["payload_json"]), "created_at_ms": int(row["created_at_ms"])}
            for row in rows
        ]

    def enqueue_message(
        self,
        message_id: str,
        destination: str,
        body: str,
        *,
        subject: str = "",
        priority: int = 0,
        expires_at_ms: int | None = None,
        enhanced_mode: str | None = None,
    ) -> None:
        now = utc_now_ms()
        self.connection.execute(
            "INSERT INTO messages(id, destination, subject, body, priority, state, expires_at_ms, "
            "created_at_ms, updated_at_ms, enhanced_mode) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
            (message_id, destination, subject, body, priority, expires_at_ms, now, now, enhanced_mode),
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

    def recent_observations(self, limit: int = 50, band: str | None = None) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(limit, 500))
        if band == "":
            return []
        where = " WHERE band = ?" if band else ""
        parameters: tuple[Any, ...] = (band.strip().lower(),) if band else ()
        rows = self.connection.execute(
            "SELECT id, event_type, value, params_json, observed_at_ms "
            ", band, dial_frequency FROM observations" + where + " ORDER BY observed_at_ms DESC LIMIT ?",
            parameters + (bounded_limit,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["params"] = json.loads(item.pop("params_json"))
            result.append(item)
        return result

    def prune_observations(self, *, now_ms: int | None = None, retention_ms: int = 7 * 24 * 60 * 60 * 1000) -> int:
        """Bound detailed RF evidence without touching mailbox or audit data."""
        if retention_ms < 60_000:
            raise ValueError("observation retention is too short")
        cutoff = (utc_now_ms() if now_ms is None else now_ms) - retention_ms
        cursor = self.connection.execute(
            "DELETE FROM observations WHERE observed_at_ms < ?", (cutoff,)
        )
        self.connection.commit()
        return int(cursor.rowcount)

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

    def record_message_path(self, message_id: str, path: tuple[str, ...]) -> None:
        if not message_id or len(path) < 2 or any(not call or len(call) > 16 for call in path):
            raise ValueError("invalid attempted message path")
        now = utc_now_ms()
        path_text = "→".join(call.upper() for call in path)
        self.connection.execute(
            "INSERT INTO message_paths(message_id, path, first_attempted_at_ms, last_attempted_at_ms) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(message_id, path) DO UPDATE SET last_attempted_at_ms=excluded.last_attempted_at_ms",
            (message_id, path_text, now, now),
        )
        self.connection.commit()

    def attempted_message_paths(self, message_id: str) -> set[tuple[str, ...]]:
        rows = self.connection.execute(
            "SELECT path FROM message_paths WHERE message_id = ?", (message_id,)
        ).fetchall()
        return {tuple(str(row[0]).split("→")) for row in rows}

    def message_paths(self, message_id: str) -> list[tuple[str, ...]]:
        rows = self.connection.execute(
            "SELECT path FROM message_paths WHERE message_id = ? ORDER BY last_attempted_at_ms DESC",
            (message_id,),
        ).fetchall()
        return [tuple(str(row[0]).split("→")) for row in rows]

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

    def custodian_score(self, station: str) -> float:
        """Return a bounded reliability score from durable custody history."""
        rows = self.connection.execute(
            "SELECT status, COUNT(*) AS count FROM custody WHERE custodian = ? GROUP BY status",
            (station.upper(),),
        ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        accepted = counts.get("accepted", 0)
        forwarded = counts.get("forwarded", 0)
        failed = counts.get("failed", 0)
        return min(1.0, (accepted + 2 * forwarded) / max(1, accepted + forwarded + failed))

    def upsert_inbox_message(
        self,
        sender: str,
        message_id: str,
        body: str,
        total_parts: int,
        received_parts: tuple[int, ...],
        complete: bool,
        path: tuple[str, ...] = (),
        group_name: str = "",
        protocol: str = "standard",
        delivery: str = "direct",
    ) -> None:
        if not sender or not message_id or not 1 <= total_parts <= 255:
            raise ValueError("invalid inbox message")
        if len(body) > 100_000 or any(not 1 <= part <= total_parts for part in received_parts):
            raise ValueError("invalid inbox message content")
        now = utc_now_ms()
        group_name = group_name.upper()[:32] if group_name.startswith("@") else ""
        protocol = "js8m" if protocol.lower() == "js8m" else "standard"
        delivery = delivery if delivery in {"direct", "forwarded", "stored_collected", "group_broadcast"} else "direct"
        self.connection.execute(
            "INSERT INTO inbox_messages(sender, message_id, body, total_parts, received_parts_json, complete, path, first_received_at_ms, updated_at_ms, group_name, protocol, delivery) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(sender, message_id) DO UPDATE SET body=excluded.body, "
            "total_parts=excluded.total_parts, received_parts_json=excluded.received_parts_json, complete=excluded.complete, "
            "path=excluded.path, updated_at_ms=excluded.updated_at_ms, group_name=excluded.group_name, protocol=excluded.protocol, delivery=excluded.delivery",
            (
                sender.upper(), message_id, body, total_parts, json.dumps(received_parts),
                int(complete), "→".join(path), now, now, group_name, protocol, delivery,
            ),
        )
        self.connection.commit()

    def list_inbox(self, group_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM inbox_messages"
        if group_only:
            query += " WHERE group_name != ''"
        query += " ORDER BY updated_at_ms DESC"
        rows = self.connection.execute(
            query
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["received_parts"] = tuple(json.loads(item.pop("received_parts_json")))
            item["complete"] = bool(item["complete"])
            result.append(item)
        return result

    def find_partial_inbox(self, sender: str, body: str) -> str | None:
        """Find a provisional legacy fragment that a later full decode can replace."""
        row = self.connection.execute(
            "SELECT message_id, body FROM inbox_messages "
            "WHERE sender = ? AND complete = 0 ORDER BY updated_at_ms DESC",
            (sender.upper(),),
        ).fetchall()
        candidate = body.strip().rstrip("…").rstrip(".").strip()
        if not candidate:
            return None
        for item in row:
            fragment = str(item["body"]).strip().rstrip("…").rstrip(".").strip()
            if fragment and (candidate.startswith(fragment) or fragment.startswith(candidate)):
                return str(item["message_id"])
        return None

    def delete_inbox_message(self, sender: str, message_id: str) -> None:
        cursor = self.connection.execute(
            "DELETE FROM inbox_messages WHERE sender = ? AND message_id = ?",
            (sender.upper(), message_id),
        )
        if cursor.rowcount != 1:
            raise KeyError(message_id)
        self.connection.commit()
        self.audit("inbox.message_removed", {"sender": sender.upper(), "message_id": message_id})

    def observe_group(self, name: str, description: str = "") -> None:
        now = utc_now_ms()
        self.connection.execute(
            "INSERT INTO groups(name, description, first_seen_at_ms, last_seen_at_ms, seen_count) VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT(name) DO UPDATE SET last_seen_at_ms=excluded.last_seen_at_ms, "
            "seen_count=groups.seen_count + 1, description=CASE WHEN groups.description='' THEN excluded.description ELSE groups.description END",
            (name.upper(), description, now, now),
        )
        self.connection.commit()

    def ensure_group(self, name: str, description: str = "") -> None:
        now = utc_now_ms()
        self.connection.execute(
            "INSERT INTO groups(name, description, first_seen_at_ms, last_seen_at_ms) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET description=CASE WHEN excluded.description != '' THEN excluded.description ELSE groups.description END",
            (name.upper(), description, now, now),
        )
        self.connection.commit()

    def list_groups(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM groups ORDER BY last_seen_at_ms DESC, name"
        ).fetchall()
        return [dict(row) for row in rows]

    def prune_groups(self, *, now_ms: int | None = None, retention_ms: int = 30 * 24 * 60 * 60 * 1000) -> int:
        if retention_ms < 60_000:
            raise ValueError("group retention is too short")
        cutoff = (utc_now_ms() if now_ms is None else now_ms) - retention_ms
        cursor = self.connection.execute(
            "DELETE FROM groups WHERE seen_count > 0 AND last_seen_at_ms < ? AND subscribed = 0",
            (cutoff,),
        )
        self.connection.commit()
        return int(cursor.rowcount)

    def set_group_subscription(self, name: str, subscribed: bool) -> None:
        self.connection.execute(
            "UPDATE groups SET subscribed = ? WHERE name = ?", (int(subscribed), name.upper())
        )
        self.connection.commit()

    def defer_message(
        self, message_id: str, delay_ms: int, detail: str, *, increment_retry: bool = True
    ) -> None:
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
            (
                MessageState.WAITING_ROUTE,
                int(row["retry_count"]) + (1 if increment_retry else 0),
                now + max(1000, delay_ms),
                now,
                message_id,
            ),
        )
        self.connection.commit()
        self.record_attempt(message_id, "defer", "route", "waiting", detail)

    def due_for_retry(self, message_id: str) -> bool:
        row = self.connection.execute(
            "SELECT next_attempt_at_ms FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        return row is not None and (row["next_attempt_at_ms"] is None or row["next_attempt_at_ms"] <= utc_now_ms())

    def wake_message_for_route(self, message_id: str) -> None:
        """Make a waiting message due without changing its retry count or state."""
        now = utc_now_ms()
        self.connection.execute(
            "UPDATE messages SET next_attempt_at_ms = NULL, updated_at_ms = ? "
            "WHERE id = ? AND state = ?",
            (now, message_id, "waiting_route"),
        )
        self.connection.commit()

    def delete_message(self, message_id: str) -> None:
        row = self.connection.execute("SELECT state FROM messages WHERE id = ?", (message_id,)).fetchone()
        if row is None:
            raise KeyError(message_id)
        if row["state"] == "in_progress":
            raise ValueError("cannot remove a message currently in progress")
        # Remove dependent history before the parent row. Several older tables
        # do not declare ON DELETE CASCADE, so deleting only attempts violates
        # SQLite foreign-key enforcement for airtime and route history.
        with self.connection:
            for table in (
                "message_attempts",
                "message_parts",
                "custody",
                "message_paths",
                "message_airtime",
            ):
                self.connection.execute(f"DELETE FROM {table} WHERE message_id = ?", (message_id,))
            self.connection.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        self.audit("message.removed", {"message_id": message_id})

    def get_configuration(self, key: str, default: str = "") -> str:
        row = self.connection.execute(
            "SELECT value FROM configuration WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row is not None else default

    def set_configuration(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO configuration(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.connection.commit()
