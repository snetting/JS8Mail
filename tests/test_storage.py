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
