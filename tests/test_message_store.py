from pathlib import Path

import pytest

from js8mail.storage import Database


def test_message_queue_and_transition_are_durable(tmp_path: Path) -> None:
    path = tmp_path / "mail.sqlite3"
    database = Database(path)
    database.enqueue_message("m-1", "N0CALL", "HELLO")
    database.transition_message("m-1", "waiting_route")
    database.close()

    reopened = Database(path)
    row = reopened.connection.execute(
        "SELECT destination, state FROM messages WHERE id = 'm-1'"
    ).fetchone()
    assert tuple(row) == ("N0CALL", "waiting_route")
    assert reopened.connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 2
    reopened.close()


def test_terminal_message_cannot_be_reopened(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    database.enqueue_message("m-1", "N0CALL", "HELLO")
    database.transition_message("m-1", "waiting_route")
    database.transition_message("m-1", "in_progress")
    database.transition_message("m-1", "delivered")
    with pytest.raises(ValueError):
        database.transition_message("m-1", "queued")
    database.close()
