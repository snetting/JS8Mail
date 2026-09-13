from pathlib import Path

import pytest

from js8mail.application.service import DEFAULT_MESSAGE_TTL_MS, MailService
from js8mail.storage import Database


def test_compose_cancel_and_retry(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    message_id = service.compose("n0call", "Test", "Hello")
    message = database.get_message(message_id)
    assert message["state"] == "queued"  # type: ignore[index]
    assert message["expires_at_ms"] - message["created_at_ms"] == DEFAULT_MESSAGE_TTL_MS  # type: ignore[index]
    service.cancel(message_id)
    assert database.get_message(message_id)["state"] == "cancelled"  # type: ignore[index]
    service.retry(message_id)
    assert database.get_message(message_id)["state"] == "queued"  # type: ignore[index]
    database.close()


def test_compose_validates_bounds(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    with pytest.raises(ValueError):
        service.compose("", "", "hello")
    with pytest.raises(ValueError):
        service.compose("N0CALL", "", "")
    database.close()
