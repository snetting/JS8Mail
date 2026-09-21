from pathlib import Path

from js8mail.domain import NormalizedEvent, utc_now_ms
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


def test_thread_connection_can_be_closed_and_reopened(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    first = database.connection
    database.close_thread_connection()
    second = database.connection
    assert second is not first
    database.close()


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


def test_partial_legacy_inbox_fragment_can_be_reconciled(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    database.upsert_inbox_message(
        "MM0ZFG", "legacy-partial-426", "TEST SELF DELIVERY …", 1, (), False
    )
    assert database.find_partial_inbox("mm0zfg", "TEST SELF DELIVERY") == "legacy-partial-426"
    assert database.list_inbox()[0]["protocol"] == "standard"
    database.close()


def test_inbox_collection_metadata_requires_matching_submission(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    database.upsert_inbox_message(
        "ORIGIN", "local-1", "hello", 1, (1,), True,
        ("ORIGIN", "CUST"), delivery="stored_collected"
    )
    database.audit(
        "inbox.retrieval_pending",
        {"custodian": "CUST", "js8call_message_id": 431, "attempt": 0},
    )
    database.audit(
        "inbox.retrieval_completed",
        {
            "custodian": "CUST",
            "js8call_message_id": 431,
            "attempt": 0,
            "message_id": "local-1",
        },
    )
    assert database.list_inbox()[0].get("custodian_message_id") is None

    database.audit(
        "inbox.retrieval_submitted",
        {"custodian": "CUST", "js8call_message_id": 431, "attempt": 1},
    )
    database.audit(
        "inbox.retrieval_completed",
        {
            "custodian": "CUST",
            "js8call_message_id": 431,
            "attempt": 1,
            "message_id": "local-1",
        },
    )
    assert database.list_inbox()[0]["custodian_message_id"] == 431
    database.close()


def test_inbox_read_state_is_durable_and_new_content_reopens_item(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    database.upsert_inbox_message("MM0SPN", "m1", "hello", 1, (1,), True)
    assert database.list_inbox()[0]["is_read"] is False
    database.mark_inbox_read("MM0SPN", "m1")
    assert database.list_inbox()[0]["is_read"] is True
    database.upsert_inbox_message("MM0SPN", "m1", "hello again", 1, (1,), True)
    assert database.list_inbox()[0]["is_read"] is False
    database.close()

    reopened = Database(tmp_path / "mail.sqlite3")
    assert reopened.list_inbox()[0]["is_read"] is False
    reopened.close()


def test_route_evidence_wakes_deferred_message_without_incrementing_retry(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    database.enqueue_message("m1", "SP2ST", "hello")
    database.defer_message("m1", 60_000, "no route")
    database.wake_message_for_route("m1")
    message = database.get_message("m1")
    assert message is not None
    assert message["state"] == "waiting_route"
    assert message["retry_count"] == 1
    assert message["next_attempt_at_ms"] is None
    assert database.due_for_retry("m1")


def test_recent_audit_events_restore_query_context(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    database.audit(
        "discovery.query_submitted",
        {
            "action": "candidate_query_call",
            "target": "MM0ZFG",
            "text": "MM0ZFG QUERY CALL SP2ST",
            "band": "20m",
        },
    )
    rows = database.recent_audit_events("discovery.query_submitted", 0)
    assert rows[-1]["payload"]["text"] == "MM0ZFG QUERY CALL SP2ST"
    assert rows[-1]["payload"]["band"] == "20m"


def test_recent_control_events_are_available_to_the_ui(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    database.audit(
        "delivery.control",
        {"label": "JS8Mail discovery", "target": "OH3SPN", "status": "submitted"},
    )
    events = database.recent_control_events()
    assert events[0]["label"] == "JS8Mail discovery"
    assert events[0]["target"] == "OH3SPN"
    database.close()


def test_airtime_accounting_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "mail.sqlite3"
    database = Database(path)
    database.save_airtime_state(1_000, 12_000, 0)
    database.enqueue_message("m1", "N0CALL", "hello")
    database.save_message_airtime("m1", 4_000)
    database.close()

    reopened = Database(path)
    assert reopened.airtime_state() == {
        "window_started_at_ms": 1_000,
        "window_used_ms": 12_000,
        "message_used_ms": 0,
    }
    assert reopened.message_airtime_used("m1") == 4_000
    reopened.close()


def test_message_parts_are_idempotent_and_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "mail.sqlite3"
    database = Database(path)
    database.upsert_message_part("m1", 2, 2, "SECOND", direction="incoming", peer="N0CALL")
    database.upsert_message_part("m1", 2, 2, "SECOND", direction="incoming", peer="N0CALL")
    assert (
        database.list_message_parts("m1", direction="incoming", peer="n0call")[0]["payload"]
        == "SECOND"
    )
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


def test_inbox_exposes_current_peer_capability_hint(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    database.upsert_inbox_message("N0CALL", "m1", "hello", 1, (1,), True)
    assert database.list_inbox()[0]["peer_js8m"] is False
    database.upsert_peer_capabilities("N0CALL", 1, ("E2E", "MP"), 9_999_999_999_999)
    assert database.list_inbox()[0]["peer_js8m"] is True
    database.close()


def test_custody_status_is_durable_and_distinct_from_delivery(tmp_path: Path) -> None:
    path = tmp_path / "mail.sqlite3"
    database = Database(path)
    database.upsert_custody("m1", "n0call", "accepted", "standard JS8Call store ACK")
    database.close()
    reopened = Database(path)
    assert reopened.list_custody("m1")[0]["status"] == "accepted"
    reopened.close()


def test_legacy_store_offer_policy_bounds_automatic_reoffers(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    database.enqueue_message("m1", "DEST", "body")
    cooldown = 2 * 60 * 1000
    quarantine = 24 * 60 * 60 * 1000
    database.record_attempt("m1", "store", "CUST", "submitted", "queued")
    now = utc_now_ms()

    waiting = database.legacy_store_offer_policy(
        "m1",
        "CUST",
        now_ms=now,
        retry_cooldown_ms=cooldown,
        max_automatic_offers=2,
        quarantine_ms=quarantine,
    )
    assert waiting["eligible"] is False
    assert waiting["next_eligible_at_ms"] >= now + cooldown - 1_000

    database.record_attempt("m1", "store_timeout", "CUST", "uncertain", "no ACK")
    due = database.legacy_store_offer_policy(
        "m1",
        "CUST",
        now_ms=utc_now_ms() + cooldown,
        retry_cooldown_ms=cooldown,
        max_automatic_offers=2,
        quarantine_ms=quarantine,
    )
    assert due["eligible"] is True
    database.record_attempt("m1", "store", "CUST", "submitted", "queued")
    bounded = database.legacy_store_offer_policy(
        "m1",
        "CUST",
        now_ms=utc_now_ms() + cooldown * 2,
        retry_cooldown_ms=cooldown,
        max_automatic_offers=2,
        quarantine_ms=quarantine,
    )
    assert bounded["eligible"] is False
    assert bounded["next_eligible_at_ms"] is None
    database.record_attempt("m1", "store_timeout", "CUST", "uncertain", "no ACK")
    quarantined = database.legacy_store_offer_policy(
        "m1",
        "CUST",
        now_ms=utc_now_ms(),
        retry_cooldown_ms=cooldown,
        max_automatic_offers=2,
        quarantine_ms=quarantine,
    )
    assert quarantined["eligible"] is False
    assert quarantined["reason"].startswith("custodian quarantined")
    assert quarantined["next_eligible_at_ms"] is not None
    after_quarantine = database.legacy_store_offer_policy(
        "m1",
        "CUST",
        now_ms=int(quarantined["next_eligible_at_ms"]) + 1,
        retry_cooldown_ms=cooldown,
        max_automatic_offers=2,
        quarantine_ms=quarantine,
    )
    assert after_quarantine["eligible"] is True
    database.close()


def test_manual_retry_overrides_legacy_store_cooldown(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    database.enqueue_message("m1", "DEST", "body")
    database.record_attempt("m1", "store", "CUST", "submitted", "queued")
    database.record_attempt("m1", "store_timeout", "CUST", "uncertain", "no ACK")
    database.record_attempt("m1", "manual_retry", "route", "requested", "operator retry")
    decision = database.legacy_store_offer_policy(
        "m1",
        "CUST",
        now_ms=utc_now_ms(),
        retry_cooldown_ms=60 * 60 * 1000,
        max_automatic_offers=1,
        quarantine_ms=24 * 60 * 60 * 1000,
    )
    assert decision["eligible"] is True
    assert decision["manual_override"] is True
    database.close()


def test_relay_route_quarantine_uses_only_completed_timeouts(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    database.enqueue_message("m1", "DEST", "body")
    path = ("ORIGIN", "RELAY", "DEST")
    first = database.begin_transmission_transaction(
        "m1", "relay", "RELAY", "DEST", path, "hash-1", 1_000, 1_000
    )
    database.mark_transmission_submitted(first)
    database.connection.execute(
        "UPDATE transmission_transactions SET status='timed_out', ack_deadline_ms=? WHERE id=?",
        (1_000, first),
    )
    second = database.begin_transmission_transaction(
        "m1", "relay", "RELAY", "DEST", path, "hash-2", 1_000, 1_000
    )
    database.mark_transmission_submitted(second)
    database.connection.execute(
        "UPDATE transmission_transactions SET status='timed_out', ack_deadline_ms=? WHERE id=?",
        (2_000, second),
    )
    database.connection.commit()
    policy = database.message_route_failure_policies(
        "m1", now_ms=2_001, quarantine_ms=86_400_000
    )
    assert policy[path]["blocked"] is True
    assert policy[path]["failure_count"] == 2

    busy = database.begin_transmission_transaction(
        "m1", "relay", "RELAY", "DEST", path, "hash-3", 1_000, 1_000
    )
    database.mark_transmission_unconfirmed(busy)
    database.connection.commit()
    assert database.message_route_failure_policies("m1", now_ms=2_001)[path]["failure_count"] == 2
    database.close()


def test_transmission_ack_correlation_survives_route_state_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "mail.sqlite3"
    database = Database(path)
    database.enqueue_message("m1", "DEST", "body")
    transaction_id = database.begin_transmission_transaction(
        "m1", "store", "CUST", "CUST", ("ORIGIN", "CUST"), "hash", 30_000, 60_000
    )
    database.mark_transmission_submitted(transaction_id)
    database.transition_message("m1", "waiting_route")
    database.close()

    reopened = Database(path)
    matches = reopened.pending_transmission_for_ack("cust", utc_now_ms())
    assert len(matches) == 1
    assert matches[0]["message_id"] == "m1"
    reopened.acknowledge_transmission(transaction_id)
    assert reopened.list_transmission_transactions("m1")[0]["status"] == "acknowledged"
    reopened.close()


def test_uppercase_wire_id_and_cumulative_part_receipts_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "mail.sqlite3"
    database = Database(path)
    database.enqueue_message("abcdef0123456789", "N0CALL", "body")
    assert database.get_message("ABCDEF0123456789") is not None
    assert database.merge_outgoing_part_receipt("abcdef0123456789", 3, (1, 3))
    assert not database.merge_outgoing_part_receipt("abcdef0123456789", 3, (1,))
    database.close()
    reopened = Database(path)
    assert reopened.outgoing_part_bitmap("abcdef0123456789", 3) == 0b101
    reopened.close()


def test_attempted_paths_and_custodian_score_are_durable(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    database.enqueue_message("m1", "DEST", "body")
    database.record_message_path("m1", ("ORIGIN", "RELAY", "CUST"))
    database.upsert_custody("m1", "CUST", "accepted")
    database.upsert_custody("m1", "CUST", "forwarded")
    database.close()
    reopened = Database(tmp_path / "mail.sqlite3")
    assert reopened.attempted_message_paths("m1") == {("ORIGIN", "RELAY", "CUST")}
    assert reopened.custodian_score("CUST") == 1.0
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


def test_remove_message_cleans_all_spool_history(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    database.enqueue_message("m1", "DEST", "body")
    database.record_attempt("m1", "direct", "DEST", "submitted")
    database.upsert_message_part("m1", 1, 1, "body", direction="outgoing", peer="DEST")
    database.upsert_custody("m1", "RELAY", "accepted")
    database.record_message_path("m1", ("ORIGIN", "RELAY", "DEST"))
    database.save_message_airtime("m1", 1000)

    database.delete_message("m1")

    assert database.get_message("m1") is None
    for table in (
        "message_attempts",
        "message_parts",
        "custody",
        "message_paths",
        "message_airtime",
    ):
        assert (
            database.connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE message_id = ?", ("m1",)
            ).fetchone()[0]
            == 0
        )
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


def test_group_subscription_persists_and_can_be_removed(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    database.ensure_group("@JS8MAIL", "discussion and updates")
    database.set_group_subscription("@JS8MAIL", True)
    assert database.list_groups()[0]["subscribed"] == 1
    database.set_group_subscription("@JS8MAIL", False)
    assert database.list_groups()[0]["subscribed"] == 0
    database.close()


def test_default_js8mail_group_is_subscribed_without_overwriting_choice(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    database.ensure_group("@JS8MAIL", "discussion and updates", subscribed=True)
    assert database.list_groups()[0]["subscribed"] == 1
    database.set_group_subscription("@JS8MAIL", False)
    database.ensure_group("@JS8MAIL", "discussion and updates", subscribed=True)
    assert database.list_groups()[0]["subscribed"] == 0
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
