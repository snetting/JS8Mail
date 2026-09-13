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
