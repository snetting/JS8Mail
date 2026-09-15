import time
from pathlib import Path

import pytest

from js8mail.application.service import DEFAULT_MESSAGE_TTL_MS, MailService
from js8mail.domain import NormalizedEvent
from js8mail.storage import Database


def test_compose_cancel_and_retry(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    message_id = service.compose("n0call", "Test", "Hello")
    message = database.get_message(message_id)
    assert message["state"] == "queued"  # type: ignore[index]
    assert DEFAULT_MESSAGE_TTL_MS - 1 <= message["expires_at_ms"] - message["created_at_ms"] <= DEFAULT_MESSAGE_TTL_MS  # type: ignore[index]
    service.cancel(message_id)
    assert database.get_message(message_id)["state"] == "cancelled"  # type: ignore[index]
    service.retry(message_id)
    assert database.get_message(message_id)["state"] == "queued"  # type: ignore[index]


def test_cancelled_message_does_not_report_active_discovery(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    message_id = service.compose("MM0ZFG", "test", "body")
    database.record_attempt(message_id, "hearing_query", "MM0ZFG", "submitted", "probe")
    service.cancel(message_id)
    view = service.message_views()[0]
    assert view["confidence"] == "cancelled"
    database.close()


def test_cancel_is_idempotent_for_terminal_or_missing_messages(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    message_id = service.compose("MM0ZFG", "test", "body")
    service.cancel(message_id)
    service.cancel(message_id)
    service.cancel("stale-row")
    assert database.get_message(message_id)["state"] == "cancelled"  # type: ignore[index]
    database.close()


def test_delete_is_idempotent_for_missing_messages(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    service.delete("stale-row")
    database.close()


def test_new_queued_message_reports_waiting_for_discovery(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    service.compose("VK5CZ", "Test", "Waiting")
    view = service.message_views()[0]
    assert view["confidence"] == "new"
    database.close()


def test_message_confidence_describes_latest_unconfirmed_operation(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    message_id = service.compose("VK5CZ", "Test", "Waiting")
    database.record_attempt(message_id, "store", "F4LPU", "submitted", "queued")
    assert service.message_views()[0]["confidence"] == "awaiting_custodian_ack"
    database.record_attempt(message_id, "store_timeout", "F4LPU", "uncertain", "no ACK")
    assert service.message_views()[0]["confidence"] == "delivery_uncertain"
    database.close()


def test_new_discovery_attempt_overrides_older_payload_submission(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    message_id = service.compose("SP2ST", "Testing", "Testing path discovery")
    database.record_attempt(message_id, "relay", "MM0ZFG", "submitted", "queued")
    database.record_attempt(message_id, "snr_probe", "SP2ST", "submitted", "waiting for RF evidence")
    database.record_attempt(message_id, "defer", "route", "waiting", "listening for probe response")
    assert service.message_views()[0]["confidence"] == "discovery_in_progress"
    database.close()


def test_compose_validates_bounds(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    with pytest.raises(ValueError):
        service.compose("", "", "hello")
    with pytest.raises(ValueError):
        service.compose("N0CALL", "", "")
    with pytest.raises(ValueError):
        service.compose("N0CALL", "", "x" * 4001)
    database.close()


def test_group_messages_are_forced_to_standard_mode(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    message_id = service.compose(
        "@EMCOMM", "alert", "plain group bulletin", enhanced_mode="required"
    )
    message = database.get_message(message_id)
    assert message is not None
    assert message["enhanced_mode"] == "standard"
    database.close()


def test_recently_heard_does_not_count_as_a_directed_answer(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    database.record_observation(NormalizedEvent("RX.ACTIVITY", "M8YRT CQ", {"FROM": "M8YRT"}, 1_000))
    assert service.recently_heard("M8YRT", now_ms=1_500, window_ms=10_000)
    assert service.recent_heard_age_ms("M8YRT", now_ms=1_500, window_ms=10_000) == 500
    assert not service.recently_answered("M8YRT", "OH3SPN", now_ms=1_500, window_ms=10_000)
    database.record_observation(
        NormalizedEvent(
            "QUERY.CALL.RESPONSE",
            "M8YRT YES",
            {"FROM": "M8YRT", "TO": "N0CALL", "EVIDENCE": "remote_query_call_yes"},
            1_200,
        )
    )
    assert service.recent_heard_age_ms("M8YRT", now_ms=1_500, window_ms=10_000) == 500
    assert service.recent_heard_age_ms("REMOTE", now_ms=1_500, window_ms=10_000) is None
    database.record_observation(
        NormalizedEvent("RX.DIRECTED.ME", "OH3SPN SNR -10", {"FROM": "M8YRT", "TO": "OH3SPN"}, 2_000)
    )
    assert service.recently_answered("M8YRT", "OH3SPN", now_ms=2_500, window_ms=10_000)
    assert service.recent_answered_age_ms("M8YRT", "OH3SPN", now_ms=2_500, window_ms=10_000) == 500
    database.close()


def test_message_graph_omits_self_links(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    message_id = service.compose("OH3SPN", "test", "body")
    database.record_attempt(message_id, "direct", "OH3SPN", "submitted", "self test")
    graph = service.message_graph(message_id, "OH3SPN")
    assert graph["edges"] == []
    database.close()


def test_live_activity_graph_requires_recent_evidence_in_both_directions(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    now = int(time.time() * 1000)
    database.record_observation(
        NormalizedEvent("RX.ACTIVITY", "F4VLF CQ", {"FROM": "F4VLF", "TO": "OH3SPN"}, now - 1_000),
        band="20m",
    )

    graph = service.live_activity_graph("20m")
    assert graph["edges"][0]["kind"] == "isolated_one_way"

    database.record_observation(
        NormalizedEvent("RX.ACTIVITY", "OH3SPN CQ", {"FROM": "OH3SPN", "TO": "F4VLF"}, now - 500),
        band="20m",
    )
    graph = service.live_activity_graph("20m")
    assert graph["edges"][0]["kind"] == "reciprocal"
    database.close()


def test_station_views_marks_live_js8m_capability(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    now = int(time.time() * 1000)
    database.record_observation(
        NormalizedEvent("RX.ACTIVITY", "F4VLF CQ", {"FROM": "F4VLF"}, now - 1_000),
        band="20m",
    )
    database.upsert_peer_capabilities("F4VLF", 1, ("E2E", "MP"), now + 60_000)

    assert service.is_js8m_capable("f4vlf") is True
    assert service.is_js8m_capable("N0CALL") is False
    station = service.station_views(band="20m")[0]
    assert station["callsign"] == "F4VLF"
    assert station["js8m"] is True
    database.close()


def test_route_planning_uses_only_requested_band(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    database.connection.execute(
        "INSERT INTO temporal_links(source, destination, band, speed, first_observed_at_ms, last_observed_at_ms, observation_count, max_snr) "
        "VALUES ('A', 'B', '40m', '0', 1000, 1000, 1, -5)"
    )
    database.connection.execute(
        "INSERT INTO temporal_links(source, destination, band, speed, first_observed_at_ms, last_observed_at_ms, observation_count, max_snr) "
        "VALUES ('B', 'C', '40m', '0', 1000, 1000, 1, -5)"
    )
    database.connection.commit()
    assert service.plan_route("A", "C", now_ms=1000, band="20m").path == ("A",)
    assert service.plan_route("A", "C", now_ms=1000, band="40m").path == ("A", "B", "C")
    database.close()


def test_query_call_yes_builds_a_conservative_candidate_route(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    database.record_link_projection(
        NormalizedEvent(
            "QUERY.CALL.REACHABILITY",
            "OH3SPN YES",
            {"FROM": "OH3SPN", "TO": "MM0ZFG", "SNR": -7},
            1_000,
        ),
        band="20m",
    )
    database.record_link_projection(
        NormalizedEvent(
            "QUERY.CALL.RESPONSE",
            "OH3SPN YES",
            {"FROM": "MM0ZFG", "TO": "SP2ST", "EVIDENCE": "remote_query_call_yes"},
            1_000,
        ),
        band="20m",
    )
    plan = service.plan_route("OH3SPN", "SP2ST", now_ms=1_000, band="20m")
    assert plan.path == ("OH3SPN", "MM0ZFG", "SP2ST")
    database.close()
