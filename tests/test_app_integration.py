import asyncio

import pytest

from js8mail.application.service import MailService
from js8mail.radio_policy import AirtimeBudget
from js8mail.routing import RoutePlan
from js8mail.storage import Database
from js8mail.tools.app import Handler, capability_outbound_ms, capability_response_window_ms


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
    assert direct == 45_000
    assert multi > direct
    assert outbound > 0


@pytest.mark.asyncio
async def test_selected_multi_hop_plan_reaches_fake_radio(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("js8mail.tools.app.AUTOMATED_TX_GAP_MS", 0)
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
    handler.auto_speed = False
    plan = RoutePlan("relay", ("OH3SPN", "MM0ZFG", "SP2ST"), 0.7, 0.6, 2_000, "fresh path")

    await handler.transmit(message_id, plan)

    assert radio.sent == [
        "MM0ZFG>SP2ST MSG J8M1 D OH3SPN SP2ST " + message_id + " 1/1 test path"
    ]
    assert database.attempted_message_paths(message_id) == {
        ("OH3SPN", "MM0ZFG", "SP2ST")
    }
    database.close()


@pytest.mark.asyncio
async def test_unknown_peer_capability_waits_then_falls_back_to_plain_message(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("js8mail.tools.app.AUTOMATED_TX_GAP_MS", 0)
    database = Database(tmp_path / "mail.sqlite3")
    service = MailService(database)
    message_id = service.compose("N0CALL", "first contact", "hello ordinary station")
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
    handler.auto_speed = False

    await handler.transmit(message_id)
    assert radio.sent == ["N0CALL J8M1 CAP 1 E2E,MP,PA"]

    database.connection.execute(
        "UPDATE message_attempts SET created_at_ms = created_at_ms - 120000 "
        "WHERE message_id = ? AND action = 'capability'",
        (message_id,),
    )
    database.connection.commit()
    await handler.transmit(message_id)

    assert radio.sent[-1] == "N0CALL MSG hello ordinary station"
    assert database.list_attempts(message_id)[-1]["status"] == "submitted"
    database.close()
