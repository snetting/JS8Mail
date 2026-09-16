"""Deterministic regressions for inter-frame PTT gaps and RF completion."""

import asyncio

import pytest

from js8mail.application.service import MailService
from js8mail.domain import utc_now_ms
from js8mail.radio_policy import AirtimeBudget
from js8mail.rf_timing import TX_TRAIN_QUIET_MS, TxTrain
from js8mail.storage import Database
from js8mail.tools.app import Handler, complete_rf_train


class HaltableRadio:
    def __init__(self) -> None:
        self.halted = False

    async def halt(self) -> None:
        self.halted = True


def test_inter_frame_ptt_gap_does_not_finish_message() -> None:
    train = TxTrain()
    train.ptt(True, 1_000)
    train.frame(2_000)
    train.ptt(False, 31_000)
    assert not train.settled(32_500)
    train.ptt(True, 32_500)
    train.frame(33_000)
    train.ptt(False, 61_000)
    assert train.on_air_ms == 58_500
    assert not train.settled(61_000 + TX_TRAIN_QUIET_MS - 1)
    assert train.settled(61_000 + TX_TRAIN_QUIET_MS)


def test_late_tx_frame_extends_but_does_not_lose_final_ptt_off() -> None:
    train = TxTrain()
    train.ptt(True, 1_000)
    train.ptt(False, 31_000)
    train.frame(35_000)
    assert not train.settled(31_000 + TX_TRAIN_QUIET_MS)
    assert train.settled(35_000 + TX_TRAIN_QUIET_MS)


def test_ack_deadline_starts_after_final_frame_not_first_gap(tmp_path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    message_id = service.compose("N0CALL", "", "payload")
    transaction_id = database.begin_transmission_transaction(
        message_id,
        "direct",
        "N0CALL",
        "N0CALL",
        ("OH3SPN", "N0CALL"),
        "hash",
        30_000,
        60_000,
    )
    database.mark_transmission_submitted(transaction_id)
    old_deadline = utc_now_ms() - 1
    database.connection.execute(
        "UPDATE transmission_transactions SET ack_deadline_ms=? WHERE id=?",
        (old_deadline, transaction_id),
    )
    database.connection.commit()
    assert database.expire_transmission_transactions() == []

    handler = object.__new__(Handler)
    handler.active_transaction_id = transaction_id
    handler.airtime_budget = AirtimeBudget()
    handler.message_budgets = {}
    handler.next_tx_not_before_ms = None
    status = {"tx_message_id": message_id, "tx_reserved_ms": 30_000, "tx_train_pending": True}
    now = utc_now_ms()
    train = TxTrain()
    train.ptt(True, now - TX_TRAIN_QUIET_MS - 4_000)
    train.ptt(False, now - TX_TRAIN_QUIET_MS - 1_000)
    assert complete_rf_train(database, handler, status, train)
    row = database.get_transmission_transaction(transaction_id)
    assert row is not None
    assert row["status"] == "awaiting_ack"
    assert row["tx_finished_at_ms"] is not None
    assert row["ack_deadline_ms"] > utc_now_ms()
    assert handler.active_transaction_id is None
    database.close()


def test_broadcast_without_rf_completion_is_not_reported_delivered(tmp_path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    message_id = service.compose("@JS8MAIL", "", "one brief notice")
    transaction_id = database.begin_transmission_transaction(
        message_id,
        "group_broadcast",
        "@JS8MAIL",
        "@JS8MAIL",
        ("OH3SPN", "@JS8MAIL"),
        "hash",
        30_000,
        60_000,
    )
    database.mark_transmission_submitted(transaction_id)
    database.transition_message(message_id, "waiting_route")
    database.transition_message(message_id, "in_progress")
    assert database.get_message(message_id)["state"] == "in_progress"
    assert database.expire_transmission_transactions(utc_now_ms() + 90_000) == []
    assert len(database.expire_unfinished_broadcasts(utc_now_ms() + 31 * 60_000)) == 1
    assert database.get_transmission_transaction(transaction_id)["status"] == "unconfirmed"
    database.close()


@pytest.mark.asyncio
async def test_enhanced_train_never_halts_at_frame_or_train_boundary(tmp_path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    message_id = service.compose("N0CALL", "", "enhanced part")
    transaction_id = database.begin_transmission_transaction(
        message_id,
        "multipart",
        "N0CALL",
        "N0CALL",
        ("OH3SPN", "N0CALL"),
        "hash",
        30_000,
        60_000,
    )
    database.mark_transmission_submitted(transaction_id)
    handler = object.__new__(Handler)
    handler.client = HaltableRadio()
    handler.active_transaction_id = transaction_id
    handler.airtime_budget = AirtimeBudget()
    handler.message_budgets = {}
    handler.next_tx_not_before_ms = None
    status = {
        "tx_message_id": message_id,
        "tx_reserved_ms": 30_000,
        "tx_train_pending": True,
        "enhanced_one_shot_message_id": message_id,
    }
    now = utc_now_ms()
    train = TxTrain()
    train.ptt(True, now - TX_TRAIN_QUIET_MS - 4_000)
    train.ptt(False, now - TX_TRAIN_QUIET_MS - 1_000)

    assert complete_rf_train(database, handler, status, train)
    await asyncio.sleep(0)
    # TX.FRAME is an early, frame-preparation notification.  Enhanced
    # traffic must be allowed to complete its complete JS8Call RF train;
    # halting here used to truncate the first frame to a few hundred ms.
    assert not handler.client.halted
    assert "enhanced_one_shot_message_id" not in status
    database.close()
