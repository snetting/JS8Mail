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


def test_compose_validates_bounds(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    with pytest.raises(ValueError):
        service.compose("", "", "hello")
    with pytest.raises(ValueError):
        service.compose("N0CALL", "", "")
    database.close()


def test_recently_heard_does_not_count_as_a_directed_answer(tmp_path: Path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    database.record_observation(NormalizedEvent("RX.ACTIVITY", "M8YRT CQ", {"FROM": "M8YRT"}, 1_000))
    assert service.recently_heard("M8YRT", now_ms=1_500, window_ms=10_000)
    assert not service.recently_answered("M8YRT", "OH3SPN", now_ms=1_500, window_ms=10_000)
    database.record_observation(
        NormalizedEvent("RX.DIRECTED.ME", "OH3SPN SNR -10", {"FROM": "M8YRT", "TO": "OH3SPN"}, 2_000)
    )
    assert service.recently_answered("M8YRT", "OH3SPN", now_ms=2_500, window_ms=10_000)
    database.close()
