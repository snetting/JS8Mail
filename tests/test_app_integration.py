import asyncio

import pytest

from js8mail.application.service import MailService
from js8mail.domain import NormalizedEvent, utc_now_ms
from js8mail.radio_policy import AirtimeBudget
from js8mail.rf_timing import TX_TRAIN_QUIET_MS, TxTrain
from js8mail.routing import RoutePlan
from js8mail.storage import Database
from js8mail.tools.app import (
    Handler,
    capability_outbound_ms,
    capability_response_window_ms,
    complete_rf_train,
    delivery_path_for_receipt,
    fresh_direct_response_age_ms,
    queue_marker_capability_response,
    reconcile_part_receipt,
    route_evidence_settling_window_ms,
)


class FakeRadio:
    connected = True

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, text: str) -> None:
        self.sent.append(text)

    async def set_speed(self, speed: int) -> None:
        return


def test_capability_timing_accounts_for_return_hops() -> None:
    direct = capability_response_window_ms(("A", "B"), 0)
    multi = capability_response_window_ms(("A", "B", "C"), 0)
    outbound = capability_outbound_ms(("A", "B", "C"), "B>C J8M1 CAP", 0)
    assert direct == 240_000
    assert multi > direct
    assert outbound > 0


def test_marked_first_contact_queues_a_delayed_capability_response(tmp_path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    pending: dict[str, int] = {}
    reasons: dict[str, str] = {}
    last_sent: dict[str, int] = {}

    assert queue_marker_capability_response(
        database,
        pending,
        reasons,
        last_sent,
        "N0CALL",
        "OH3SPN",
        marker_seen=True,
        message_complete=True,
        collected=False,
        addressed_to_local=True,
        now_ms=10_000_000,
    )
    assert pending["N0CALL"] == 10_015_000
    assert reasons["N0CALL"] == "first-contact JS8Mail marker"

    # The pending response and the one-hour cooldown prevent repeated CAP
    # replies when a sender retransmits the same ordinary message.
    assert not queue_marker_capability_response(
        database,
        pending,
        reasons,
        last_sent,
        "N0CALL",
        "OH3SPN",
        marker_seen=True,
        message_complete=True,
        collected=False,
        addressed_to_local=True,
        now_ms=10_000_001,
    )
    assert not queue_marker_capability_response(
        database,
        {},
        {},
        {"N0CALL": 7_000_000},
        "N0CALL",
        "OH3SPN",
        marker_seen=True,
        message_complete=True,
        collected=False,
        addressed_to_local=True,
        now_ms=10_000_001,
    )
    assert not queue_marker_capability_response(
        database,
        {},
        {},
        {},
        "N0CALL",
        "OH3SPN",
        marker_seen=True,
        message_complete=False,
        collected=False,
        addressed_to_local=True,
        now_ms=10_000_000,
    )
    database.close()


def test_route_evidence_settling_window_is_bounded() -> None:
    assert route_evidence_settling_window_ms(105_000, 0) == 70_000
    assert route_evidence_settling_window_ms(45_000, 0) == 45_000
    assert route_evidence_settling_window_ms(180_000, 8) == 30_000


def test_fresh_direct_response_guard_uses_current_band(tmp_path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    database.record_observation(
        NormalizedEvent(
            "RX.DIRECTED",
            "M0SPN SNR +17",
            {"FROM": "OH3SPN", "TO": "M0SPN", "CMD": "SNR", "SNR": 17},
            10_000,
        ),
        band="20m",
    )

    assert fresh_direct_response_age_ms(service, "OH3SPN", "M0SPN", "20m", now_ms=11_000) == 1_000
    assert fresh_direct_response_age_ms(service, "OH3SPN", "M0SPN", "40m", now_ms=11_000) is None
    database.close()


@pytest.mark.asyncio
async def test_selected_multi_hop_plan_reaches_fake_radio(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("js8mail.tools.app.AUTOMATED_TX_GAP_MS", 0)
    monkeypatch.setattr("js8mail.tools.app.AUTOMATED_RX_WINDOW_MS", 0)
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    message_id = service.compose("SP2ST", "route", "test path")
    database.upsert_peer_capabilities("SP2ST", 1, ("E2E", "MP"), 9_999_999_999_999)
    radio = FakeRadio()
    handler = object.__new__(Handler)
    handler.service = service
    handler.client = radio
    handler.status = {
        "callsign": "OH3SPN",
        "tx_mode": "automatic",
        "paused": False,
        "speed": 0,
        "band": "20m",
        "js8_activity_until_ms": 0,
    }
    handler.announced_destinations = set()
    handler.airtime_budget = AirtimeBudget()
    handler.message_budgets = {}
    handler.tx_lock = asyncio.Lock()
    handler.last_tx_at_ms = None
    handler.next_tx_not_before_ms = None
    handler.auto_speed = False
    plan = RoutePlan("relay", ("OH3SPN", "MM0ZFG", "SP2ST"), 0.7, 0.6, 2_000, "fresh path")

    await handler.transmit(message_id, plan)

    assert radio.sent == ["MM0ZFG>SP2ST>MSG J8M1 D " + message_id + " 1/1 {S:route}| test path"]
    assert database.attempted_message_paths(message_id) == {("OH3SPN", "MM0ZFG", "SP2ST")}
    database.close()


@pytest.mark.asyncio
async def test_group_broadcast_is_terminal_without_ack_wait(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("js8mail.tools.app.AUTOMATED_TX_GAP_MS", 0)
    monkeypatch.setattr("js8mail.tools.app.AUTOMATED_RX_WINDOW_MS", 0)
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    message_id = service.compose("@JS8MAIL", "group test", "hello group", enhanced_mode="required")
    radio = FakeRadio()
    handler = object.__new__(Handler)
    handler.service = service
    handler.client = radio
    handler.status = {
        "callsign": "OH3SPN",
        "tx_mode": "automatic",
        "paused": False,
        "speed": 0,
        "band": "20m",
        "js8_activity_until_ms": 0,
    }
    handler.announced_destinations = set()
    handler.airtime_budget = AirtimeBudget()
    handler.message_budgets = {}
    handler.tx_lock = asyncio.Lock()
    handler.last_tx_at_ms = None
    handler.next_tx_not_before_ms = None
    handler.auto_speed = False

    await handler.transmit(message_id)

    assert radio.sent == ["@JS8MAIL MSG group test: hello group"]
    assert database.get_message(message_id)["state"] == "in_progress"  # type: ignore[index]
    now = utc_now_ms()
    train = TxTrain()
    train.ptt(True, now - TX_TRAIN_QUIET_MS - 3_000)
    train.ptt(False, now - TX_TRAIN_QUIET_MS - 1_000)
    assert complete_rf_train(database, handler, handler.status, train)
    assert database.get_message(message_id)["state"] == "delivered"  # type: ignore[index]
    assert service.message_views()[0]["confidence"] == "broadcast_submitted"
    assert not any(
        attempt["action"] == "capability" for attempt in database.list_attempts(message_id)
    )
    database.close()


@pytest.mark.asyncio
async def test_tx_yields_to_incoming_directed_message(tmp_path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    radio = FakeRadio()
    handler = object.__new__(Handler)
    handler.service = service
    handler.client = radio
    handler.status = {
        "tx_mode": "automatic",
        "paused": False,
        "incoming_directed_until_ms": 9_999_999_999_999,
    }
    handler.airtime_budget = AirtimeBudget()
    handler.message_budgets = {}
    handler.tx_lock = asyncio.Lock()
    handler.last_tx_at_ms = None
    handler.next_tx_not_before_ms = None

    with pytest.raises(RuntimeError, match="receiving a directed message"):
        await handler.send_rf("@ALLCALL MSG queued broadcast")
    assert radio.sent == []
    database.close()


@pytest.mark.asyncio
async def test_tx_yields_to_recent_unclassified_rf_activity(tmp_path) -> None:
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    radio = FakeRadio()
    handler = object.__new__(Handler)
    handler.service = service
    handler.client = radio
    handler.status = {
        "tx_mode": "automatic",
        "paused": False,
        "incoming_activity_until_ms": 9_999_999_999_999,
    }
    handler.airtime_budget = AirtimeBudget()
    handler.message_budgets = {}
    handler.tx_lock = asyncio.Lock()
    handler.last_tx_at_ms = None
    handler.next_tx_not_before_ms = None

    with pytest.raises(RuntimeError, match="recently decoded RF activity"):
        await handler.send_rf("@ALLCALL MSG queued broadcast")
    assert radio.sent == []
    database.close()


@pytest.mark.asyncio
async def test_stale_multi_hop_route_probes_first_hop_before_payload(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("js8mail.tools.app.AUTOMATED_TX_GAP_MS", 0)
    monkeypatch.setattr("js8mail.tools.app.AUTOMATED_RX_WINDOW_MS", 0)
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    message_id = service.compose("N3IDR", "route", "long payload")
    radio = FakeRadio()
    handler = object.__new__(Handler)
    handler.service = service
    handler.client = radio
    handler.status = {
        "callsign": "OH3SPN",
        "tx_mode": "automatic",
        "paused": False,
        "speed": 0,
        "band": "20m",
        "js8_activity_until_ms": 0,
    }
    handler.announced_destinations = set()
    handler.airtime_budget = AirtimeBudget()
    handler.message_budgets = {}
    handler.tx_lock = asyncio.Lock()
    handler.last_tx_at_ms = None
    handler.next_tx_not_before_ms = None
    handler.auto_speed = False

    allowed = await handler.ensure_route_first_hop_reachable(
        message_id, ("OH3SPN", "IZ1KJG", "N3IDR")
    )

    assert allowed is False
    assert radio.sent == ["IZ1KJG SNR?"]
    assert any(
        attempt["action"] == "route_probe" and attempt["status"] == "submitted"
        for attempt in database.list_attempts(message_id)
    )
    database.close()


@pytest.mark.asyncio
async def test_unknown_peer_capability_waits_then_falls_back_to_plain_message(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("js8mail.tools.app.AUTOMATED_TX_GAP_MS", 0)
    monkeypatch.setattr("js8mail.tools.app.AUTOMATED_RX_WINDOW_MS", 0)
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    message_id = service.compose(
        "N0CALL", "first contact", "hello ordinary station", enhanced_mode="required"
    )
    radio = FakeRadio()
    handler = object.__new__(Handler)
    handler.service = service
    handler.client = radio
    handler.status = {
        "callsign": "OH3SPN",
        "tx_mode": "automatic",
        "paused": False,
        "speed": 0,
        "band": "20m",
        "js8_activity_until_ms": 0,
    }
    handler.announced_destinations = set()
    handler.airtime_budget = AirtimeBudget()
    handler.message_budgets = {}
    handler.tx_lock = asyncio.Lock()
    handler.last_tx_at_ms = None
    handler.next_tx_not_before_ms = None
    handler.auto_speed = False

    await handler.transmit(message_id)
    assert radio.sent == ["N0CALL J8M1 CAP 1 E2E,MP,PA"]

    database.connection.execute(
        "UPDATE message_attempts SET created_at_ms = created_at_ms - 360000 "
        "WHERE message_id = ? AND action = 'capability'",
        (message_id,),
    )
    database.record_attempt(message_id, "capability_tx", "N0CALL", "complete", "RF finished")
    database.connection.execute(
        "UPDATE message_attempts SET created_at_ms = created_at_ms - 300000 "
        "WHERE message_id = ? AND action = 'capability_tx'",
        (message_id,),
    )
    database.connection.commit()
    handler.next_tx_not_before_ms = 0
    await handler.transmit(message_id)

    assert radio.sent[-1] == "N0CALL MSG first contact: hello ordinary station"
    assert database.list_attempts(message_id)[-1]["status"] == "submitted"
    database.close()


@pytest.mark.asyncio
async def test_multipart_sender_waits_for_part_ack_before_next_part(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("js8mail.tools.app.AUTOMATED_TX_GAP_MS", 0)
    monkeypatch.setattr("js8mail.tools.app.AUTOMATED_RX_WINDOW_MS", 0)
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    message_id = service.compose("N0CALL", "RF", "A" * 400)
    database.upsert_peer_capabilities("N0CALL", 1, ("E2E", "MP", "PA"), 9_999_999_999_999)
    radio = FakeRadio()
    handler = object.__new__(Handler)
    handler.service = service
    handler.client = radio
    handler.status = {
        "callsign": "OH3SPN",
        "tx_mode": "automatic",
        "paused": False,
        "speed": 0,
        "band": "20m",
    }
    handler.announced_destinations = set()
    handler.airtime_budget = AirtimeBudget()
    handler.message_budgets = {}
    handler.tx_lock = asyncio.Lock()
    handler.last_tx_at_ms = None
    handler.next_tx_not_before_ms = None
    handler.auto_speed = False

    await handler.transmit(message_id)
    assert len(radio.sent) == 1
    assert " 1/3 " in radio.sent[0]
    first_tx = database.list_transmission_transactions(message_id)[-1]
    database.acknowledge_transmission(int(first_tx["id"]))
    assert database.merge_outgoing_part_receipt(message_id, 3, (1,))
    database.transition_message(message_id, "waiting_route")
    handler.next_tx_not_before_ms = 0
    await handler.transmit(message_id)
    assert len(radio.sent) == 2
    assert " 2/3 " in radio.sent[1]
    database.close()


def test_receipt_path_is_origin_to_destination() -> None:
    assert delivery_path_for_receipt("OH3SPN", "M0SPN", ()) == ("OH3SPN", "M0SPN")
    assert delivery_path_for_receipt("OH3SPN", "M0SPN", ("M0SPN", "R1", "OH3SPN")) == (
        "OH3SPN",
        "R1",
        "M0SPN",
    )


def test_part_ack_advances_durable_stop_and_wait_without_transmitting(tmp_path) -> None:
    from js8mail.protocol import parse_part_ack

    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    message_id = service.compose("N0CALL", "", "A" * 400)
    database.transition_message(message_id, "waiting_route")
    database.transition_message(message_id, "in_progress")
    transaction_id = database.begin_transmission_transaction(
        message_id,
        "multipart",
        "N0CALL",
        "N0CALL",
        ("OH3SPN", "N0CALL"),
        "hash",
        30_000,
        480_000,
    )
    database.mark_transmission_submitted(transaction_id)
    receipt = parse_part_ack(f"J8M1 PA {message_id.upper()} 3 1")
    assert receipt is not None
    assert not reconcile_part_receipt(database, database.get_message(message_id), receipt, "OTHER")
    assert reconcile_part_receipt(database, database.get_message(message_id), receipt, "N0CALL")
    assert database.get_message(message_id)["state"] == "waiting_route"
    assert database.get_transmission_transaction(transaction_id)["status"] == "acknowledged"
    assert database.outgoing_part_bitmap(message_id, 3) == 1
    assert not reconcile_part_receipt(database, database.get_message(message_id), receipt, "N0CALL")
    database.close()


@pytest.mark.asyncio
async def test_opportunistic_unknown_peer_sends_plain_message_without_capability_probe(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("js8mail.tools.app.AUTOMATED_TX_GAP_MS", 0)
    monkeypatch.setattr("js8mail.tools.app.AUTOMATED_RX_WINDOW_MS", 0)
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    message_id = service.compose(
        "N0CALL", "first contact", "hello ordinary station", enhanced_mode="opportunistic"
    )
    radio = FakeRadio()
    handler = object.__new__(Handler)
    handler.service = service
    handler.client = radio
    handler.status = {
        "callsign": "OH3SPN",
        "tx_mode": "automatic",
        "paused": False,
        "speed": 0,
        "band": "20m",
        "js8_activity_until_ms": 0,
    }
    handler.announced_destinations = set()
    handler.airtime_budget = AirtimeBudget()
    handler.message_budgets = {}
    handler.tx_lock = asyncio.Lock()
    handler.last_tx_at_ms = None
    handler.next_tx_not_before_ms = None
    handler.auto_speed = False

    await handler.transmit(message_id)

    assert radio.sent == ["N0CALL MSG first contact: hello ordinary station [JS8Mail/0.0.8]"]
    assert not any(
        attempt["action"] == "capability_wait" for attempt in database.list_attempts(message_id)
    )
    database.close()


@pytest.mark.asyncio
async def test_store_offer_uses_readable_text_for_unknown_custodian(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("js8mail.tools.app.AUTOMATED_TX_GAP_MS", 0)
    monkeypatch.setattr("js8mail.tools.app.AUTOMATED_RX_WINDOW_MS", 0)
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    message_id = service.compose(
        "N0CALL", "first contact", "hello ordinary station", enhanced_mode="opportunistic"
    )
    radio = FakeRadio()
    handler = object.__new__(Handler)
    handler.service = service
    handler.client = radio
    handler.status = {
        "callsign": "OH3SPN",
        "tx_mode": "automatic",
        "paused": False,
        "speed": 0,
        "band": "20m",
        "js8_activity_until_ms": 0,
    }
    handler.airtime_budget = AirtimeBudget()
    handler.message_budgets = {}
    handler.tx_lock = asyncio.Lock()
    handler.last_tx_at_ms = None
    handler.next_tx_not_before_ms = None
    handler.auto_speed = False

    await handler.transmit_store(message_id, "CUST")

    assert radio.sent == ["CUST MSG TO:N0CALL first contact: hello ordinary station"]
    assert "standard-readable" in database.list_attempts(message_id)[-1]["detail"]
    database.close()


@pytest.mark.asyncio
async def test_store_offer_uses_enhanced_envelope_for_capable_custodian(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("js8mail.tools.app.AUTOMATED_TX_GAP_MS", 0)
    monkeypatch.setattr("js8mail.tools.app.AUTOMATED_RX_WINDOW_MS", 0)
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    message_id = service.compose(
        "N0CALL", "enhanced", "hello enhanced station", enhanced_mode="opportunistic"
    )
    database.upsert_peer_capabilities("CUST", 1, ("E2E", "MP", "PA"), 9_999_999_999_999)
    radio = FakeRadio()
    handler = object.__new__(Handler)
    handler.service = service
    handler.client = radio
    handler.status = {
        "callsign": "OH3SPN",
        "tx_mode": "automatic",
        "paused": False,
        "speed": 0,
        "band": "20m",
        "js8_activity_until_ms": 0,
    }
    handler.airtime_budget = AirtimeBudget()
    handler.message_budgets = {}
    handler.tx_lock = asyncio.Lock()
    handler.last_tx_at_ms = None
    handler.next_tx_not_before_ms = None
    handler.auto_speed = False

    await handler.transmit_store(message_id, "CUST")

    assert radio.sent == [
        (
            f"CUST MSG TO:N0CALL J8M1 D OH3SPN N0CALL {message_id} 1/1 "
            "{S:enhanced}| hello enhanced station"
        )
    ]
    assert "JS8Mail store" in database.list_attempts(message_id)[-1]["detail"]
    database.close()


@pytest.mark.asyncio
async def test_standard_mode_sends_plain_message_without_capability_probe(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("js8mail.tools.app.AUTOMATED_TX_GAP_MS", 0)
    monkeypatch.setattr("js8mail.tools.app.AUTOMATED_RX_WINDOW_MS", 0)
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    message_id = service.compose("N0CALL", "standard", "hello", enhanced_mode="standard")
    radio = FakeRadio()
    handler = object.__new__(Handler)
    handler.service = service
    handler.client = radio
    handler.status = {
        "callsign": "OH3SPN",
        "tx_mode": "automatic",
        "paused": False,
        "speed": 0,
        "band": "20m",
        "js8_activity_until_ms": 0,
        "enhanced_mode": "standard",
    }
    handler.announced_destinations = set()
    handler.airtime_budget = AirtimeBudget()
    handler.message_budgets = {}
    handler.tx_lock = asyncio.Lock()
    handler.last_tx_at_ms = None
    handler.next_tx_not_before_ms = None
    handler.auto_speed = False

    await handler.transmit(message_id)

    assert len(radio.sent) == 1
    assert radio.sent[0].startswith("N0CALL MSG ")
    assert "CAP" not in radio.sent[0]
    assert "JS8Mail" not in radio.sent[0]
    assert radio.sent[0].endswith(" hello")
    assert all(attempt["action"] != "capability" for attempt in database.list_attempts(message_id))
    database.close()
