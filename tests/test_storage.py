from pathlib import Path

from js8mail.domain import NormalizedEvent
from js8mail.storage import Database


def test_observation_and_audit_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "mail.sqlite3"
    database = Database(path)
    database.record_observation(
        NormalizedEvent("RX.ACTIVITY", "N0CALL: HI", {"SNR": -10}, 1_700_000_000_000)
    )
    database.audit("probe.connected", {"host": "127.0.0.1"})
    database.close()

    reopened = Database(path)
    assert reopened.connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    assert reopened.connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 1
    reopened.close()


def test_message_retry_is_durable_and_progressively_scheduled(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    database.enqueue_message("m1", "N0CALL", "hello")
    database.defer_message("m1", 60_000, "no route")
    message = database.get_message("m1")
    assert message is not None
    assert message["state"] == "waiting_route"
    assert message["retry_count"] == 1
    assert message["next_attempt_at_ms"] > message["updated_at_ms"]
    assert not database.due_for_retry("m1")
    assert database.list_attempts("m1")[-1]["action"] == "defer"
    database.close()

    reopened = Database(tmp_path / "mail.sqlite3")
    assert reopened.get_message("m1")["retry_count"] == 1  # type: ignore[index]
    reopened.close()


def test_message_parts_are_idempotent_and_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "mail.sqlite3"
    database = Database(path)
    database.upsert_message_part("m1", 2, 2, "SECOND", direction="incoming", peer="N0CALL")
    database.upsert_message_part("m1", 2, 2, "SECOND", direction="incoming", peer="N0CALL")
    assert database.list_message_parts("m1", direction="incoming", peer="n0call")[0]["payload"] == "SECOND"
    database.close()
    reopened = Database(path)
    assert len(reopened.list_message_parts("m1", direction="incoming", peer="N0CALL")) == 1
    reopened.close()


def test_peer_capabilities_expire_and_are_durable(tmp_path: Path) -> None:
    path = tmp_path / "mail.sqlite3"
    database = Database(path)
    database.upsert_peer_capabilities("n0call", 1, ("E2E", "MP"), 9_999_999_999_999)
    assert database.peer_capabilities("N0CALL") == (1, ("E2E", "MP"))
    database.close()
    reopened = Database(path)
    assert reopened.peer_capabilities("N0CALL") == (1, ("E2E", "MP"))
    reopened.close()


def test_custody_status_is_durable_and_distinct_from_delivery(tmp_path: Path) -> None:
    path = tmp_path / "mail.sqlite3"
    database = Database(path)
    database.upsert_custody("m1", "n0call", "accepted", "standard JS8Call store ACK")
    database.close()
    reopened = Database(path)
    assert reopened.list_custody("m1")[0]["status"] == "accepted"
    reopened.close()


def test_partial_inbox_message_is_updated_idempotently(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    database.upsert_inbox_message("n0call", "m1", "A[MISSING PART 2/2]", 2, (1,), False)
    database.upsert_inbox_message("n0call", "m1", "AB", 2, (1, 2), True, ("N0CALL", "ME"))
    inbox = database.list_inbox()
    assert len(inbox) == 1
    assert inbox[0]["complete"] is True
    assert inbox[0]["received_parts"] == (1, 2)
    database.close()


def test_group_alerts_can_be_listed_separately(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    database.upsert_inbox_message("n0call", "m1", "ALERT", 1, (1,), True, group_name="@EMCOMM")
    database.upsert_inbox_message("n0call", "m2", "private", 1, (1,), True)
    assert [item["message_id"] for item in database.list_inbox(group_only=True)] == ["m1"]
    assert len(database.list_inbox()) == 2
    database.close()


def test_observation_retention_does_not_remove_audit_or_mail(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    database.record_observation(NormalizedEvent("OLD", "", {}, 1_000))
    database.audit("test.keep", {})
    database.enqueue_message("m1", "N0CALL", "hello")
    assert database.prune_observations(now_ms=100_000, retention_ms=60_000) == 1
    assert database.recent_observations() == []
    assert database.get_message("m1") is not None
    assert database.connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 2
    database.close()


def test_default_and_observed_groups_are_catalogued(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    database.ensure_group("@EMCOMM", "emergency communications")
    database.observe_group("@EMCOMM", "emergency communications")
    groups = database.list_groups()
    assert groups[0]["name"] == "@EMCOMM"
    assert groups[0]["seen_count"] == 1
    assert groups[0]["subscribed"] == 0
    database.close()


def test_unsubscribed_stale_groups_expire_but_catalog_groups_remain(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    database.ensure_group("@EMCOMM", "emergency communications")
    database.observe_group("@OLD", "observed group")
    database.connection.execute("UPDATE groups SET last_seen_at_ms = 1 WHERE name = '@OLD'")
    database.connection.commit()
    assert database.prune_groups(now_ms=31 * 24 * 60 * 60 * 1000) == 1
    assert "@OLD" not in {item["name"] for item in database.list_groups()}
    assert "@EMCOMM" in {item["name"] for item in database.list_groups()}
    database.close()


def test_link_projection_persists_sessions_and_aggregates(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    event = NormalizedEvent(
        "RX.DIRECTED", "", {"FROM": "A", "TO": "B", "SNR": -8, "BAND": "20M", "SPEED": "1"}, 1_000
    )
    database.record_link_projection(event)
    database.record_link_projection(event)
    assert database.temporal_link_views()[0]["observation_count"] == 2
    assert database.connection.execute("SELECT COUNT(*) FROM station_sessions").fetchone()[0] == 2
    database.close()
