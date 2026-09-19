"""Run the local JS8Mail mailbox and daemon."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import threading
import traceback
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from js8mail.adapters.js8call.client import Js8CallClient
from js8mail.adapters.js8call.protocol import normalize_directed_event, parse_legacy_ack
from js8mail.application.lifecycle import MessageState
from js8mail.application.service import ENHANCED_MODES, MailService
from js8mail.bands import band_from_frequency_hz, context_from_params
from js8mail.discovery import (
    PendingCallQuery,
    QueryScheduler,
    call_query,
    correlate_query_call_response,
    messages_query,
    parse_messages_available_context,
    parse_query_call_response,
    retrieve_message_query,
    snr_query,
)
from js8mail.domain import NormalizedEvent, utc_now_ms
from js8mail.groups import DEFAULT_GROUPS, default_group_description, extract_groups
from js8mail.protocol import (
    CAPABILITY_TTL_MS,
    MessagePart,
    MultipartAccumulator,
    PartReceipt,
    clean_user_message,
    contains_js8mail_marker,
    extract_envelope_subject,
    extract_js8mail_control,
    find_capability,
    format_capability,
    format_delivery_ack,
    format_human_data_part,
    format_ordinary_message,
    format_part_ack,
    format_relay_message,
    format_relay_text,
    format_standard_user_payload,
    format_store_message,
    is_js8mail_wire_frame,
    parse_ack,
    parse_capability,
    parse_delivery_ack,
    parse_human_data_part,
    parse_part_ack,
    parse_resend_request,
    split_human_message,
)
from js8mail.radio_policy import (
    SPEED_AIRTIME_MS,
    AdaptiveSpeedPolicy,
    AirtimeBudget,
    AirtimeBudgetExceeded,
    SpeedEvidence,
    estimate_airtime_ms,
)
from js8mail.reassembly import ActivityAssembler, ActivityFragment
from js8mail.rf_timing import TX_TRAIN_QUIET_MS, TxTrain
from js8mail.routing import RouteAction, RoutePlan
from js8mail.storage import Database

DIRECT_RESPONSE_DEADLINE_MS = 2 * 60 * 1000
ENHANCED_RECEIPT_DEADLINE_MS = 8 * 60 * 1000
CAPABILITY_RESPONSE_DEADLINE_MS = 4 * 60 * 1000
AUTOMATED_TX_GAP_MS = 30 * 1000
AUTOMATED_RX_WINDOW_MS = 60 * 1000
# A decode event can arrive shortly after the RF frame that caused it. Keep
# autonomous traffic out of one normal JS8Call receive window at a time, but
# allow at most three consecutive unrelated receive slots before permitting
# our queued TX. This prevents a busy band from extending the RX hold forever.
RX_ACTIVITY_GUARD_MS = 15 * 1000
MAX_RX_ACTIVITY_GUARD_SLOTS = 3
QUERY_RESPONSE_MAX_MS = 3 * 60 * 1000
LATE_QUERY_CONTEXT_MS = 15 * 60 * 1000
CAPABILITY_MAX_RESPONSE_MS = 12 * 60 * 1000
CAPABILITY_TX_START_GRACE_MS = 10 * 60 * 1000
CAPABILITY_RESPONSE_COOLDOWN_MS = 60 * 60 * 1000
MARKER_CAPABILITY_RESPONSE_DELAY_MS = 15 * 1000
ENHANCED_ACK_RETRY_LIMIT = 2
ACK_DUPLICATE_WINDOW_MS = 30 * 1000
# Historical graph evidence is useful for choosing which route to investigate,
# but it is not sufficient justification for submitting a long relay payload.
# Require a fresh answer from the first hop before spending that airtime.
ROUTE_HOP_PROBE_FRESH_MS = 10 * 60 * 1000


def radio_exception_reason(exc: BaseException) -> str:
    """Return an operator-facing handoff reason instead of a bare exception name."""
    detail = str(exc).strip()
    if detail:
        return detail
    if isinstance(exc, RuntimeError):
        return "JS8Call handoff unavailable"
    return type(exc).__name__


def update_rx_activity_guard(
    last_activity_ms: int,
    guard_slots: int,
    now_ms: int,
    current_until_ms: int,
) -> tuple[int, int]:
    """Advance a bounded generic RX hold without extending it per decode."""
    if last_activity_ms and now_ms - last_activity_ms < RX_ACTIVITY_GUARD_MS:
        return current_until_ms, guard_slots
    if (
        not last_activity_ms
        or now_ms - last_activity_ms >= RX_ACTIVITY_GUARD_MS * MAX_RX_ACTIVITY_GUARD_SLOTS
    ):
        guard_slots = 0
    if guard_slots >= MAX_RX_ACTIVITY_GUARD_SLOTS:
        return current_until_ms, guard_slots
    return now_ms + RX_ACTIVITY_GUARD_MS, guard_slots + 1
ROUTE_HOP_PROBE_COOLDOWN_MS = 5 * 60 * 1000
# A message may use a bounded burst, then continue in later rolling windows.
# The total is intentionally larger than the three-day default message TTL;
# the station-wide budget remains the ultimate safety ceiling.
MESSAGE_BURST_LIMIT_MS = 10 * 60 * 1000
MESSAGE_TOTAL_LIMIT_MS = 60 * 60 * 1000
# JS8Call v3.0.3 sends a normal ACK after accepting MSG TO:, but the protocol
# does not make that ACK an end-to-end custody receipt. Allow one short retry
# after an ambiguous offer, then quarantine the same custodian for a day. The
# quarantine is per custodian and an operator retry remains an override.
LEGACY_CUSTODY_RETRY_COOLDOWN_MS = 2 * 60 * 1000
LEGACY_CUSTODY_MAX_AUTOMATIC_OFFERS = 2
LEGACY_CUSTODY_ALTERNATE_DISCOVERY_DELAY_MS = 2 * 60 * 1000
LEGACY_CUSTODY_FAILURE_QUARANTINE_MS = 24 * 60 * 60 * 1000


def capability_response_window_ms(path: tuple[str, ...], speed: object) -> int:
    """Estimate the return-path response window after CAP arrives."""
    try:
        speed_id = int(str(speed))
    except (TypeError, ValueError):
        speed_id = 0
    cycle_ms = SPEED_AIRTIME_MS.get(speed_id, SPEED_AIRTIME_MS[0])
    hops = max(1, len(path) - 1)
    # A complete CAP response can span several RF frames, even on one hop.
    # Start this window after the outbound CAP train has actually finished.
    return min(
        CAPABILITY_MAX_RESPONSE_MS,
        max(CAPABILITY_RESPONSE_DEADLINE_MS, 3 * cycle_ms + 60_000) + (hops - 1) * 2 * cycle_ms,
    )


def delivery_path_for_receipt(
    sender: str, recipient: str, observed_path: tuple[str, ...]
) -> tuple[str, ...]:
    """Put receipt metadata in the message's origin-to-destination order."""
    sender, recipient = sender.upper(), recipient.upper()
    path = tuple(call.upper() for call in observed_path if call)
    if path and path[0] == recipient and path[-1] == sender:
        path = tuple(reversed(path))
    if path and path[0] == sender and path[-1] == recipient:
        return path
    return (sender, recipient)


def capability_outbound_ms(path: tuple[str, ...], text: str, speed: object) -> int:
    """Estimate the time for a CAP frame to reach the final hop."""
    try:
        speed_id = int(str(speed))
    except (TypeError, ValueError):
        speed_id = 0
    return max(1, len(path) - 1) * estimate_airtime_ms(text, speed_id)


def queue_marker_capability_response(
    database: Database,
    pending: dict[str, int],
    reasons: dict[str, str],
    capability_last_sent: dict[str, int],
    source: str | None,
    local_call: str,
    *,
    marker_seen: bool,
    message_complete: bool,
    collected: bool,
    addressed_to_local: bool,
    pending_capability_peer: str = "",
    now_ms: int | None = None,
) -> bool:
    """Queue one safe CAP response to a marked first-contact message.

    The readable version marker is deliberately not a feature claim. It is a
    low-cost invitation for a JS8Mail receiver to identify itself after the
    ordinary message has settled. The caller's scheduler performs the actual
    RF handoff, preserving the receive/ACK opportunity and allowing the
    original Standard delivery to remain valid if the CAP is lost.
    """
    if not marker_seen or not message_complete or collected or not addressed_to_local:
        return False
    peer = str(source or "").strip().upper()
    local = str(local_call or "").strip().upper()
    if not peer or not local or peer == local or peer.startswith("@"):
        return False
    if peer in pending or str(pending_capability_peer or "").strip().upper() == peer:
        return False
    now = utc_now_ms() if now_ms is None else int(now_ms)
    if now - int(capability_last_sent.get(peer, 0) or 0) < CAPABILITY_RESPONSE_COOLDOWN_MS:
        return False
    if database.peer_capabilities(peer) is not None:
        return False
    pending[peer] = now + MARKER_CAPABILITY_RESPONSE_DELAY_MS
    reasons[peer] = "first-contact JS8Mail marker"
    database.audit(
        "peer.capability_marker_response_queued",
        {
            "peer": peer,
            "local_call": local,
            "reason": reasons[peer],
            "delay_ms": MARKER_CAPABILITY_RESPONSE_DELAY_MS,
        },
    )
    return True


def query_response_window_ms(action: str, speed: object) -> int:
    """Estimate how long a query may need to collect JS8Call replies."""
    try:
        speed_id = int(str(speed))
    except (TypeError, ValueError):
        speed_id = 0
    cycle_ms = SPEED_AIRTIME_MS.get(speed_id, SPEED_AIRTIME_MS[0])
    cycles = 3 if action == "allcall_query_call" else 2
    return min(QUERY_RESPONSE_MAX_MS, max(60_000, cycles * cycle_ms + 15_000))


def fresh_direct_response_age_ms(
    service: MailService,
    destination: str,
    local_call: str,
    band: str,
    now_ms: int | None = None,
) -> int | None:
    """Return fresh direct reachability evidence for a destination.

    Discovery is asynchronous: an RX event can be persisted while the
    scheduler is already processing an older snapshot of the same message.
    Keep this check in one small helper so every decision point uses the same
    band-aware freshness rule, including the final guard before a broadcast
    query is submitted.
    """
    return service.recent_answered_age_ms(
        destination,
        local_call,
        now_ms=now_ms,
        window_ms=ROUTE_HOP_PROBE_FRESH_MS,
        band=band,
    )


def reverse_custody_path(
    local_call: str,
    original_sender: str,
    incoming_path: tuple[str, ...],
) -> tuple[str, ...]:
    """Build a safe reverse path when the received path identifies the origin."""
    local = local_call.strip().upper()
    original = original_sender.strip().upper()
    path = tuple(item.strip().upper() for item in incoming_path if item.strip())
    if not local or not original or local not in path:
        return ()
    local_index = path.index(local)
    forward_prefix = path[: local_index + 1]
    if not forward_prefix or forward_prefix[0] != original:
        return ()
    reverse = (local, *reversed(forward_prefix[:-1]))
    return reverse if len(reverse) >= 2 and reverse[-1] == original else ()


ROUTE_EVIDENCE_SETTLE_MAX_MS = 90_000
ROUTE_EVIDENCE_SETTLE_GUARD_MS = 10_000


def route_evidence_settling_window_ms(response_window_ms: int, speed: object) -> int:
    """Allow one or two expected RF cycles for competing query replies."""
    try:
        speed_id = int(str(speed))
    except (TypeError, ValueError):
        speed_id = 0
    cycle_ms = SPEED_AIRTIME_MS.get(speed_id, SPEED_AIRTIME_MS[0])
    expected = 2 * cycle_ms + ROUTE_EVIDENCE_SETTLE_GUARD_MS
    return min(response_window_ms, ROUTE_EVIDENCE_SETTLE_MAX_MS, max(30_000, expected))


def delivery_response_window_ms(operation: str, path: tuple[str, ...], speed: object) -> int:
    """Bound the wait for a legacy ACK after the real TX has ended.

    Direct and custodian ACKs normally arrive in the next one or two cycles.
    A relayed final ACK has to cross the reverse path, so the allowance grows
    with hop count but remains bounded.  This is deliberately a response
    deadline, not an airtime estimate; the latter is stored separately.
    """
    try:
        speed_id = int(str(speed))
    except (TypeError, ValueError):
        speed_id = 0
    cycle_ms = SPEED_AIRTIME_MS.get(speed_id, SPEED_AIRTIME_MS[0])
    hops = max(1, len(path) - 1)
    if operation == "multipart":
        return max(ENHANCED_RECEIPT_DEADLINE_MS, (hops + 2) * cycle_ms)
    if operation == "relay":
        return min(10 * 60 * 1000, max(60_000, (hops + 1) * cycle_ms + 30_000))
    return min(3 * 60 * 1000, max(45_000, 2 * cycle_ms + 15_000))


def distinct_ack_count(attempts: list[dict[str, Any]], target: str) -> int:
    """Count separate final-peer ACK opportunities, coalescing duplicate decodes."""
    timestamps: list[int] = []
    for attempt in attempts:
        if (
            attempt["action"] != "standard_ack"
            or attempt["status"] != "received"
            or str(attempt["target"]).upper() != target.upper()
        ):
            continue
        created_at_ms = int(attempt["created_at_ms"])
        if not timestamps or created_at_ms - timestamps[-1] > ACK_DUPLICATE_WINDOW_MS:
            timestamps.append(created_at_ms)
    return len(timestamps)


def _recent_outbound_transaction(
    database: Database, source: str, now_ms: int
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Find one durable, unambiguous legacy transaction for an ACK.

    The parent message may already be waiting for a later route, and a late
    ACK may arrive after the first deadline.  Matching the durable transaction
    rather than message state preserves both cases.  If two live operations
    target the same responder, refuse to guess.
    """
    candidates = database.pending_transmission_for_ack(source, now_ms)
    live = [
        item for item in candidates if item["status"] in {"queued", "tx_active", "awaiting_ack"}
    ]
    if len(live) > 1:
        return None
    chosen = live or candidates
    if not chosen:
        return None
    if len(chosen) > 1 and int(chosen[0]["created_at_ms"]) == int(chosen[1]["created_at_ms"]):
        return None
    transaction = max(chosen, key=lambda item: int(item["created_at_ms"]))
    message = database.get_message(str(transaction["message_id"]))
    if message is None:
        return None
    return message, transaction


def complete_rf_train(
    database: Database, controller: Handler, status: dict[str, Any], train: TxTrain
) -> bool:
    """Reconcile the final RF frame, airtime and any pending reply clock."""
    if not train.settled(utc_now_ms()):
        return False
    actual_ms = train.on_air_ms
    reserved_ms = int(status.get("tx_reserved_ms") or 0)
    message_on_air = status.get("tx_message_id")
    if message_on_air and actual_ms > reserved_ms:
        extra_ms = actual_ms - reserved_ms
        now = utc_now_ms()
        controller.airtime_budget.rollover(now)
        controller.airtime_budget.window_used_ms += extra_ms
        if controller.airtime_budget.message_limit_ms is not None:
            controller.airtime_budget.message_used_ms += extra_ms
        budget = controller.message_budgets.get(str(message_on_air))
        if budget is not None:
            budget.rollover(now)
            budget.window_used_ms += extra_ms
            budget.message_used_ms += extra_ms
            database.save_message_airtime(str(message_on_air), budget.message_used_ms)
        database.save_airtime_state(
            controller.airtime_budget.window_started_at_ms,
            controller.airtime_budget.window_used_ms,
            controller.airtime_budget.message_used_ms,
        )
    database.audit(
        "radio.tx_train_completed",
        {"message_id": message_on_air, "on_air_ms": actual_ms, "reserved_ms": reserved_ms},
    )
    transaction_id = controller.active_transaction_id
    if transaction_id is not None:
        transaction = database.get_transmission_transaction(transaction_id)
        if transaction is not None and transaction["operation"] == "group_broadcast":
            database.complete_broadcast_transmission(transaction_id)
            message_id = str(transaction["message_id"])
            message = database.get_message(message_id)
            if message is not None and message["state"] == MessageState.IN_PROGRESS:
                database.record_attempt(
                    message_id,
                    "group_broadcast",
                    str(transaction["target"]),
                    "complete",
                    "RF broadcast completed; no recipient ACK expected",
                )
                database.transition_message(message_id, MessageState.DELIVERED)
        else:
            database.finish_transmission(transaction_id)
        controller.active_transaction_id = None
    capability_message_id = status.get("pending_capability_message_id")
    if capability_message_id:
        database.record_attempt(
            str(capability_message_id),
            "capability_tx",
            str(status.get("pending_capability_peer") or ""),
            "complete",
            "CAP RF transmission completed; response window begins now",
        )
        status["pending_capability_message_id"] = None
        status["pending_capability_peer"] = None
    # TX.FRAME is emitted when JS8Call prepares a frame, not when the RF
    # frame has finished.  Never issue RIG.TX_HALT from this lifecycle: it can
    # truncate the first frame of an enhanced message and it would also
    # interrupt a legitimate multi-frame JS8Call transmission.  The complete
    # RF train is bounded by TxTrain's quiet period above; receipt deadlines
    # begin only after this point.
    status.pop("enhanced_one_shot_message_id", None)
    status.pop("enhanced_retry_halt_sent", None)
    controller.next_tx_not_before_ms = utc_now_ms() + AUTOMATED_RX_WINDOW_MS
    status["next_tx_not_before_ms"] = controller.next_tx_not_before_ms
    status["tx_message_id"] = None
    status["tx_reserved_ms"] = 0
    status["tx_train_pending"] = False
    status["incoming_activity_until_ms"] = 0
    status["last_rx_activity_ms"] = 0
    status["rx_activity_guard_slots"] = 0
    train.reset()
    return True


def reconcile_part_receipt(
    database: Database, message: dict[str, Any], receipt: PartReceipt, source: str
) -> bool:
    """Durably accept a cumulative PA and wake only the next missing part."""
    message_id = str(message["id"])
    if source.upper() != str(message["destination"]).upper():
        database.audit(
            "message.part_ack_ignored", {"message_id": message_id, "reason": "unexpected source"}
        )
        return False
    parts = split_human_message(message_id, str(message["body"]))
    if receipt.total != len(parts):
        database.audit(
            "message.part_ack_ignored", {"message_id": message_id, "reason": "total mismatch"}
        )
        return False
    changed = database.merge_outgoing_part_receipt(message_id, receipt.total, receipt.received)
    detail = f"received {len(receipt.received)}/{receipt.total} parts"
    if receipt.missing:
        detail += f"; pending {','.join(map(str, receipt.missing))}"
    database.record_attempt(message_id, "hop_ack", source, "received", detail)
    if (
        changed
        and receipt.missing
        and message["state"] in {MessageState.IN_PROGRESS, MessageState.WAITING_ROUTE}
    ):
        transactions = database.list_transmission_transactions(message_id)
        if transactions and transactions[-1]["status"] in {"queued", "tx_active", "awaiting_ack"}:
            database.acknowledge_transmission(int(transactions[-1]["id"]))
        if message["state"] == MessageState.IN_PROGRESS:
            database.transition_message(message_id, MessageState.WAITING_ROUTE)
        database.wake_message_for_route(message_id)
    return changed


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>JS8Mail</title><style>
body{font:15px system-ui;max-width:1250px;margin:2em auto;padding:0 1em;background:#f5f7f9;color:#18222d}.topbar{position:sticky;top:0;z-index:10;background:#f5f7f9;padding:.35em 0 .5em}.topline{display:flex;justify-content:space-between;align-items:center;gap:1em}.topline h1{margin:.2em 0}.version-button{white-space:nowrap;background:#334155;font-size:12px}
 .workspace{display:grid;grid-template-columns:minmax(0,1fr) minmax(280px,.8fr);gap:1em;align-items:stretch}.workspace section{margin:0;min-width:0}.workspace>section{min-height:260px}.inbox-panel{max-height:360px;overflow:auto}.live-panel{min-height:300px}.live-panel svg{width:100%;min-height:280px;background:#fbfcfd;border-radius:6px}.stations-panel{grid-column:2;grid-row:2 / span 2}.groups-panel{grid-column:1}#messages th:nth-child(2),#messages td:nth-child(2){width:8em}#messages th:nth-child(4),#messages td:nth-child(4){width:17em;white-space:nowrap}.inbox-bulk-bar{position:sticky;top:0;z-index:2;display:flex;flex-wrap:wrap;align-items:center;gap:.45em;padding:.45em .55em;margin-bottom:.5em;background:#f7f9fb;border:1px solid #d9e0e7;border-radius:6px}.inbox-bulk-bar label{display:inline-flex;align-items:center;gap:.35em;white-space:nowrap;font-weight:600}.inbox-bulk-bar button{margin:0;padding:.35em .6em}.inbox-bulk-bar button:disabled{opacity:.45;cursor:not-allowed}.inbox-bulk-count{color:#53606d;white-space:nowrap}.inbox-select-cell{width:2.25em;text-align:center;vertical-align:middle}.inbox-select-cell input{width:auto;margin:0;transform:scale(1.1);accent-color:#1769aa}.inbox-row-selected{background:#e0f2fe!important;box-shadow:inset 3px 0 #1769aa}.inbox-row-selected .inbox-select-cell{background:#bae6fd}@media(max-width:800px){.workspace{display:block}.workspace>section{margin:1em 0}.stations-panel{grid-column:auto;grid-row:auto}#messages th:nth-child(4),#messages td:nth-child(4){width:auto;white-space:normal}}
section{background:white;border:1px solid #d9e0e7;border-radius:10px;padding:1em;margin:1em 0}input,textarea,select{box-sizing:border-box;width:100%;padding:.5em;margin:.25em 0 .7em}textarea{height:110px}button{background:#1769aa;color:#fff;border:0;border-radius:5px;padding:.5em .8em;margin:.2em;cursor:pointer}.danger{background:#a33}.pill{display:inline-block;padding:.3em .6em;border-radius:1em;background:#e8edf2;margin:.2em}.ok{background:#d8f3dc}.warn{background:#fff1c2}.state-pill{display:inline-block;padding:.3em .6em;border-radius:1em;margin:.2em;font-weight:600;white-space:nowrap}.state-in-progress{background:#dbeafe;color:#174ea6}.state-acknowledged{background:#fff1c2;color:#7a4b00}.state-complete{background:#d8f3dc;color:#176b35}.state-complete-plus{background:#b7f0d0;color:#075c38}.state-failed{background:#ffd9d9;color:#8b1e1e}.state-cancelled,.state-expired{background:#e8edf2;color:#53606d}#status .pill:nth-child(3){display:none}.mono{font:12px monospace;white-space:pre-wrap;overflow-wrap:anywhere}table{width:100%;table-layout:fixed}svg{display:block;max-width:100%;height:auto}td,th{text-align:left;border-bottom:1px solid #e4e9ee;padding:.5em;vertical-align:top;overflow-wrap:anywhere}#messages th:nth-child(4),#messages td:nth-child(4){width:18em;white-space:normal}.outbox-actions{display:flex;flex-wrap:wrap;gap:.3em;align-items:flex-start}.outbox-actions button{margin:0;padding:.4em .55em;white-space:nowrap}details summary{cursor:pointer;padding:.25em 0}details summary::marker{color:#1769aa}
</style><div class=topbar><div class=topline><h1>JS8Mail</h1><button class=version-button onclick="showVersionInfo()">v0.0.8d · Updates</button></div><p>Resilient radio mail for reliable offline comms · by <a href='https://www.oh3spn.fi' target=_blank rel=noopener>OH3SPN</a> <button onclick="useStation('OH3SPN')">Compose to OH3SPN</button></p><section><div id=status>Loading…</div><div id=radio-leds class=leds><span id=led-rx class='led on-rx'>RX</span><span id=led-dcd class='led'>DCD</span><span id=led-tx class='led'>TX</span><span id=led-err class='led'>ERR</span><span id=led-js8 class='led'>JS8</span></div></section></div><style>.leds{display:inline-flex;gap:.3em;margin-left:.5em;vertical-align:middle}.led{padding:.25em .5em;border-radius:1em;background:#e8edf2;color:#53606d;font-size:12px;font-weight:600}.led.on-rx{background:#d8f3dc;color:#176b35}.led.on-tx{background:#ffd9d9;color:#8b1e1e}.led.on-dcd{background:#fff1c2;color:#785500}.led.on-err{background:#8b1e1e;color:white}#status .pill:nth-child(3){display:none}</style>
<section class=system-panel><h2>System sending mode</h2><p><label>Default JS8M sending mode <select name=enhanced_mode id=default-enhanced-mode title='Default for new messages'><option value=opportunistic>Opportunistic (recommended)</option><option value=standard>Standard JS8Call</option><option value=required>Required JS8M</option></select></label></p><small>This is the default for new directed messages. Standard uses ordinary JS8Call immediately; Opportunistic sends marked ordinary mail to unknown peers and seeks capability with a delayed CAP response, while using JS8M for known capable stations; Required waits for a JS8M capability response. Group broadcasts always use Standard.</small></section>
<div class=workspace><section class=compose-panel><h2>Compose</h2><form id=compose>Destination<input name=destination maxlength=16 required placeholder=N0CALL>Subject<input name=subject maxlength=120>Message<textarea name=body maxlength=4096 required></textarea>Priority<select name=priority><option value=0>Normal</option><option value=1>High</option><option value=2>Urgent</option><option value=3>Emergency</option></select>Message mode<select name=enhanced_mode title='Override the default for this message'><option value=''>Use system default</option><option value=standard>Standard JS8Call</option><option value=opportunistic>Opportunistic</option><option value=required>Required JS8M</option></select><button>Queue locally</button></form><span id=result></span></section>
<section class=live-panel><h2>Live RF Activity <small id=live-graph-meta></small></h2><div id=live-graph><p>Waiting for active-band observations.</p></div></section>
<section class=inbox-panel><h2>Inbox</h2><div id=inbox>Loading…</div></section>
<section class=stations-panel><h2>Recently heard stations</h2><input id=station-search type=search placeholder='Filter callsigns or evidence'><div id=stations>Loading…</div></section>
<section class=groups-panel><h2>Groups and emergency alerts</h2><p>Compose to a group or review received broadcasts. Automatic forwarding remains opt-in.</p><div id=groups>Loading…</div><h3>Group alert inbox</h3><div id=alerts>Loading…</div></section></div>
<section><h2>Outbox</h2><div id=control-events></div><div id=messages>Loading…</div></section>
<section><h2>Message route graph</h2><div id=graph-result>Select Graph on a message to inspect its evidence and attempts.</div></section>
<section><h2>Recent observations</h2><div id=observations>Loading…</div></section>
<script>
function formatAttemptDetail(value){return String(value??'').replace(/delivered_at=(\\d{10,})/g,(_,raw)=>{let date=new Date(Number(raw));if(Number.isNaN(date.getTime()))return `delivered_at=${raw}`;let local=new Intl.DateTimeFormat(undefined,{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',timeZoneName:'short'}).format(date);let utc=date.toISOString().replace('T',' ').replace(/\\.\\d{3}Z$/,' UTC');return `delivered_at=${local} / ${utc}`})}
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(u,o){let r=await fetch(u,o),j=await r.json();if(!r.ok)throw Error(j.error||r.status);return j}
function showVersionInfo(){let modal=document.getElementById('version-modal');if(!modal){modal=document.createElement('div');modal.id='version-modal';modal.innerHTML='<div class="modal-card" role="dialog" aria-modal="true"><button class="danger modal-close" onclick="closeVersionInfo()">Close</button><h2>JS8Mail v0.0.8d</h2><p>Recent improvements in this release:</p><ul><li>Reassembles long standard and JS8Mail messages from JS8Call activity frames.</li><li>Uses JS8Call first/last frame flags and keeps partial messages visible when a section is missing.</li><li>Separates interleaved receive streams and refuses ambiguous or out-of-order completion.</li><li>Improves live RF evidence rendering and forwarded-message origin handling.</li><li>Opportunistic mode actively learns JS8M capability from marked first contact, while Standard mode remains a complete one-message opt-out.</li><li>Group broadcasts omit the JS8Mail marker to save airtime; capability records last seven days and refresh with valid JS8M evidence.</li><li>Thanks to the operators who helped with the local two-station and on-air testing behind this release.</li></ul><p><small>Early development release · experimental RF software.</small></p></div>';document.body.appendChild(modal)}modal.style.display='flex'}function closeVersionInfo(){let modal=document.getElementById('version-modal');if(modal)modal.style.display='none'}
const confidenceName={new:'New · waiting for discovery',uncertain:'No delivery evidence',discovery_in_progress:'Discovery in progress',submitted_to_js8call:'Queued in JS8Call · awaiting TX',awaiting_delivery_ack:'TX submitted · awaiting delivery ACK',awaiting_custodian_ack:'Store offer submitted · awaiting custodian ACK',delivery_uncertain:'Delivery unconfirmed · retry pending',stored_at_custodian:'Delivered to custodian',radio_acknowledged:'Standard',enhanced_acknowledged:'JS8Call ACK · awaiting JS8Mail receipt',enhanced_acknowledged_stopped:'JS8Call accepted · JS8Mail receipt unconfirmed',delivered_to_js8mail:'Delivered to JS8Mail client',broadcast_submitted:'Broadcast complete · no ACK expected',cancelled:'Cancelled',delivery_failed:'Delivery failed',expired:'Expired'};
function statePill(x){if(x.tx_active)return `<span class='state-pill state-failed'>In progress · TX</span>`;if(x.confidence==='delivered_to_js8mail')return `<span class='state-pill state-complete-plus'>Complete+</span>`;if(x.state==='delivered')return `<span class='state-pill state-complete'>Complete</span>`;if(x.state==='stored')return `<span class='state-pill state-complete'>Stored</span>`;if(x.state==='acknowledged')return `<span class='state-pill state-acknowledged'>Acknowledged</span>`;if(['failed','expired','cancelled'].includes(x.state))return `<span class='state-pill state-${esc(x.state)}'>${esc(x.state[0].toUpperCase()+x.state.slice(1))}</span>`;if(x.state==='queued'&&!(x.attempts||[]).length)return `<span class='state-pill state-in-progress'>New</span>`;if(['in_progress','waiting_route','queued'].includes(x.state))return `<span class='state-pill state-in-progress'>In progress</span>`;return `<span class='state-pill'>${esc(x.state)}</span>`}
function relativeAge(seconds){seconds=Math.max(0,Number(seconds)||0);if(seconds<60)return `${Math.round(seconds)}s ago`;if(seconds<600)return `${Math.floor(seconds/60)}m ${Math.floor(seconds%60)}s ago`;if(seconds<3600)return `${Math.floor(seconds/60)}m ago`;if(seconds<86400)return `${Math.floor(seconds/3600)}h ago`;return `${Math.floor(seconds/86400)}d ago`}function evidenceLabel(value){return value==='direct'||value==='remote_report'?'Direct':value==='reported_target'?'Remote':value}
async function showGraph(id){try{let s=await api('/api/status'),g=await api('/api/graph?message_id='+encodeURIComponent(id)+'&origin='+encodeURIComponent(s.callsign||''));let cols=Math.min(4,Math.max(1,g.nodes.length)),rows=Math.max(1,Math.ceil(g.nodes.length/cols)),w=Math.max(720,cols*250+120),h=rows*120+100,nodes=g.nodes,pos={};nodes.forEach((n,i)=>pos[n]={x:60+(i%cols)*250,y:70+Math.floor(i/cols)*120});let edges=g.edges.map(e=>{let a=pos[e.from],b=pos[e.to];return `<line x1=${a.x} y1=${a.y} x2=${b.x} y2=${b.y} stroke='${e.kind==='confirmed'?'#17823b':e.kind==='attempted'?'#c77800':'#78909c'}' stroke-width=3 marker-end='url(#arrow)'/><text x=${(a.x+b.x)/2} y=${(a.y+b.y)/2-6} font-size=12>${esc(e.kind)}${e.snr!=null?' '+esc(e.snr)+'dB':''}</text>`}).join('');let circles=nodes.map(n=>`<circle cx=${pos[n].x} cy=${pos[n].y} r=28 fill='${n===g.origin?'#1769aa':n===g.destination?'#a33':'#e8edf2'}' stroke='#18222d'/><text x=${pos[n].x} y=${pos[n].y+4} text-anchor=middle font-size=12 fill='${n===g.origin||n===g.destination?'white':'#18222d'}'>${esc(n)}</text>`).join('');document.getElementById('graph-result').innerHTML=`<p><b>${esc(g.origin)} → ${esc(g.destination)}</b> · green confirmed, orange attempted, grey observed</p><svg viewBox='0 0 ${w} ${h}' width='100%' height='auto' preserveAspectRatio='xMidYMin meet' role='img' aria-label='Message route graph'><defs><marker id=arrow markerWidth=8 markerHeight=8 refX=6 refY=3 orient=auto><path d='M0,0 L0,6 L7,3 z' fill='#555'/></marker></defs>${edges}${circles}</svg>`}catch(e){document.getElementById('graph-result').textContent=e}}
const baseShowGraph=showGraph;showGraph=async id=>{await baseShowGraph(id);let svg=document.querySelector('#graph-result svg');if(!svg)return;svg.querySelectorAll('line').forEach((line,index)=>{let x1=Number(line.getAttribute('x1')),y1=Number(line.getAttribute('y1')),x2=Number(line.getAttribute('x2')),y2=Number(line.getAttribute('y2'));if(!Number.isFinite(x1)||!Number.isFinite(y1)||!Number.isFinite(x2)||!Number.isFinite(y2))return;let bend=(index%2?1:-1)*Math.min(28,Math.max(10,Math.hypot(x2-x1,y2-y1)/10)),mx=(x1+x2)/2,my=(y1+y2)/2,curve=document.createElementNS('http://www.w3.org/2000/svg','path');curve.setAttribute('d',`M${x1} ${y1} Q${mx-bend} ${my+bend} ${x2} ${y2}`);curve.setAttribute('stroke',line.getAttribute('stroke')||'#78909c');curve.setAttribute('stroke-width',line.getAttribute('stroke-width')||'3');curve.setAttribute('marker-end','url(#arrow)');curve.setAttribute('fill','none');line.replaceWith(curve)})};
// Straight edges plus explicit endpoint highlighting are easier to read than
// curved edges when several stations share a route graph.
showGraph=async id=>{await baseShowGraph(id);try{let s=await api('/api/status'),g=await api('/api/graph?message_id='+encodeURIComponent(id)+'&origin='+encodeURIComponent(s.callsign||'')),circles=[...document.querySelectorAll('#graph-result svg circle')];circles.forEach((circle,index)=>{let node=g.nodes[index],edges=g.edges.filter(e=>e.from===node||e.to===node),kind=edges.some(e=>e.kind==='confirmed')?'confirmed':edges.some(e=>e.kind==='attempted')?'attempted':'observed',color=kind==='confirmed'?'#17823b':kind==='attempted'?'#c77800':'#78909c';circle.setAttribute('stroke',color);circle.setAttribute('stroke-width',kind==='observed'?'2':'5');let title=document.createElementNS('http://www.w3.org/2000/svg','title');title.textContent=kind==='confirmed'?'Confirmed route endpoint':kind==='attempted'?'Attempted route endpoint':'Observed RF node';circle.appendChild(title)})}catch(e){}};
// Message-route graph enhancement: keep edge labels readable even when
// evidence edges cross, and size nodes for long callsigns without shrinking
// the existing 12px label text.
function graphNodeRadius(name){return Math.max(32,Math.min(48,15+String(name).length*3.8))}
function graphEdgeLabel(edge){let kind=edge.kind==='confirmed'?'confirmed':edge.kind==='attempted'?'attempted':'observed';let raw=edge.snr;let snr=raw==null?'':` ${Number.isInteger(Number(raw))?Number(raw):Number(raw).toFixed(1)} dB`;return kind+snr}
function graphBoxesOverlap(a,b){return !(a.right<b.left||a.left>b.right||a.bottom<b.top||a.top>b.bottom)}
function graphLabelPlacement(edge,index,pos,occupied){let a=pos[edge.from],b=pos[edge.to],dx=b.x-a.x,dy=b.y-a.y,length=Math.max(1,Math.hypot(dx,dy)),nx=-dy/length,ny=dx/length,label=graphEdgeLabel(edge),width=Math.max(62,Math.min(150,label.length*6.4+14)),height=20,ts=[.34,.5,.66,.24,.76],offsets=[-25,25,-42,42,0,-60,60];for(let t of ts)for(let offset of offsets){let x=a.x+dx*t+nx*offset,y=a.y+dy*t+ny*offset,box={left:x-width/2-4,right:x+width/2+4,top:y-16,bottom:y+8};if(!occupied.some(other=>graphBoxesOverlap(box,other)))return {x,y,width,height,box}}let t=.5,offset=((index%5)-2)*24,x=a.x+dx*t+nx*offset,y=a.y+dy*t+ny*offset;return {x,y,width,height,box:{left:x-width/2-4,right:x+width/2+4,top:y-16,bottom:y+8}}}
showGraph=async id=>{try{let s=await api('/api/status'),g=await api('/api/graph?message_id='+encodeURIComponent(id)+'&origin='+encodeURIComponent(s.callsign||'')),nodes=g.nodes||[],radiusByNode=Object.fromEntries(nodes.map(node=>[node,graphNodeRadius(node)])),maxRadius=Math.max(32,...nodes.map(node=>radiusByNode[node])),cols=Math.min(4,Math.max(1,nodes.length)),rows=Math.max(1,Math.ceil(nodes.length/cols)),gapX=290,gapY=155,padX=maxRadius+24,padY=maxRadius+28,w=Math.max(760,padX*2+(cols-1)*gapX),h=Math.max(190,padY*2+(rows-1)*gapY),pos={};nodes.forEach((node,index)=>pos[node]={x:padX+(index%cols)*gapX,y:padY+Math.floor(index/cols)*gapY});let occupied=nodes.map(node=>{let r=radiusByNode[node],p=pos[node];return {left:p.x-r-7,right:p.x+r+7,top:p.y-r-7,bottom:p.y+r+7}}),edges=(g.edges||[]).map((edge,index)=>{let a=pos[edge.from],b=pos[edge.to],color=edge.kind==='confirmed'?'#17823b':edge.kind==='attempted'?'#c77800':'#78909c',placement=graphLabelPlacement(edge,index,pos,occupied);occupied.push(placement.box);return `<line x1=${a.x} y1=${a.y} x2=${b.x} y2=${b.y} stroke='${color}' stroke-width='3' marker-end='url(#arrow)'/><g class=graph-edge-label><rect x=${placement.x-placement.width/2} y=${placement.y-15} width=${placement.width} height=${placement.height} rx=5 fill='#fff' stroke='#d9e0e7' stroke-width='.7' opacity='.94'/><text x=${placement.x} y=${placement.y} text-anchor=middle dominant-baseline=middle font-size=12 fill='#18222d'>${esc(graphEdgeLabel(edge))}</text></g>`}).join(''),circles=nodes.map(node=>{let p=pos[node],r=radiusByNode[node],fill=node===g.origin?'#1769aa':node===g.destination?'#a33':'#e8edf2',textFill=node===g.origin||node===g.destination?'white':'#18222d',kind=(g.edges||[]).filter(edge=>edge.from===node||edge.to===node).some(edge=>edge.kind==='confirmed')?'confirmed':(g.edges||[]).filter(edge=>edge.from===node||edge.to===node).some(edge=>edge.kind==='attempted')?'attempted':'observed',stroke=kind==='confirmed'?'#17823b':kind==='attempted'?'#c77800':'#78909c';return `<circle cx=${p.x} cy=${p.y} r=${r} fill='${fill}' stroke='${stroke}' stroke-width=${kind==='observed'?2:5}/><text x=${p.x} y=${p.y+4} text-anchor=middle font-size=12 fill='${textFill}'>${esc(node)}</text>`}).join('');document.getElementById('graph-result').innerHTML=`<p><b>${esc(g.origin)} → ${esc(g.destination)}</b> · green confirmed, orange attempted, grey observed</p><svg viewBox='0 0 ${w} ${h}' width='100%' height='auto' preserveAspectRatio='xMidYMin meet' role='img' aria-label='Message route graph'><defs><marker id=arrow markerWidth=8 markerHeight=8 refX=6 refY=3 orient=auto><path d='M0,0 L0,6 L7,3 z' fill='#555'/></marker></defs>${edges}${circles}</svg>`}catch(e){document.getElementById('graph-result').textContent=e}};
// Dense graphs need a collision-free fallback as well as the normal
// midpoint candidates; never knowingly place a label over another label.
function graphLabelPlacement(edge,index,pos,occupied){let a=pos[edge.from],b=pos[edge.to],dx=b.x-a.x,dy=b.y-a.y,length=Math.max(1,Math.hypot(dx,dy)),nx=-dy/length,ny=dx/length,label=graphEdgeLabel(edge),width=Math.max(62,Math.min(150,label.length*6.4+14)),height=20,ts=[.22,.34,.46,.58,.70,.82];for(let k=0;k<120;k++){let t=ts[k%ts.length],ring=Math.floor(k/ts.length),offset=ring===0?0:(ring%2?-1:1)*(24+Math.ceil(ring/2)*24),x=a.x+dx*t+nx*offset,y=a.y+dy*t+ny*offset,box={left:x-width/2-4,right:x+width/2+4,top:y-16,bottom:y+8};if(!occupied.some(other=>graphBoxesOverlap(box,other)))return {x,y,width,height,box}}let x=a.x+dx*.5+nx*((index+1)*180),y=a.y+dy*.5+ny*((index+1)*180),box={left:x-width/2-4,right:x+width/2+4,top:y-16,bottom:y+8};return {x,y,width,height,box}}
// HTML parsing treats SVG self-closing elements unreliably when they are
// assigned through innerHTML. Keep explicit closing tags here so long
// callsigns remain visible inside their dynamically sized nodes.
showGraph=async id=>{try{let s=await api('/api/status'),g=await api('/api/graph?message_id='+encodeURIComponent(id)+'&origin='+encodeURIComponent(s.callsign||'')),nodes=g.nodes||[],edgesData=g.edges||[],radiusByNode=Object.fromEntries(nodes.map(node=>[node,graphNodeRadius(node)])),maxRadius=Math.max(32,...nodes.map(node=>radiusByNode[node])),cols=Math.min(4,Math.max(1,nodes.length)),rows=Math.max(1,Math.ceil(nodes.length/cols)),gapX=290,gapY=155,padX=maxRadius+24,padY=maxRadius+28,w=Math.max(760,padX*2+(cols-1)*gapX),h=Math.max(190,padY*2+(rows-1)*gapY),pos={};nodes.forEach((node,index)=>pos[node]={x:padX+(index%cols)*gapX,y:padY+Math.floor(index/cols)*gapY});let occupied=nodes.map(node=>{let r=radiusByNode[node],p=pos[node];return {left:p.x-r-7,right:p.x+r+7,top:p.y-r-7,bottom:p.y+r+7}}),edges=edgesData.map((edge,index)=>{let a=pos[edge.from],b=pos[edge.to];if(!a||!b)return '';let color=edge.kind==='confirmed'?'#17823b':edge.kind==='attempted'?'#c77800':'#78909c',placement=graphLabelPlacement(edge,index,pos,occupied);occupied.push(placement.box);return `<line x1='${a.x}' y1='${a.y}' x2='${b.x}' y2='${b.y}' stroke='${color}' stroke-width='3' marker-end='url(#arrow)'></line><g class='graph-edge-label'><rect x='${placement.x-placement.width/2}' y='${placement.y-15}' width='${placement.width}' height='${placement.height}' rx='5' fill='#fff' stroke='#d9e0e7' stroke-width='.7' opacity='.94'></rect><text x='${placement.x}' y='${placement.y}' text-anchor='middle' dominant-baseline='middle' font-size='12' fill='#18222d'>${esc(graphEdgeLabel(edge))}</text></g>`}).join(''),circles=nodes.map(node=>{let p=pos[node],r=radiusByNode[node],incident=edgesData.filter(edge=>edge.from===node||edge.to===node),kind=incident.some(edge=>edge.kind==='confirmed')?'confirmed':incident.some(edge=>edge.kind==='attempted')?'attempted':'observed',stroke=kind==='confirmed'?'#17823b':kind==='attempted'?'#c77800':'#78909c',fill=node===g.origin?'#1769aa':node===g.destination?'#a33':'#e8edf2',textFill=node===g.origin||node===g.destination?'white':'#18222d';return `<circle cx='${p.x}' cy='${p.y}' r='${r}' fill='${fill}' stroke='${stroke}' stroke-width='${kind==='observed'?2:5}'></circle><text x='${p.x}' y='${p.y+4}' text-anchor='middle' font-size='12' fill='${textFill}'>${esc(node)}</text>`}).join('');document.getElementById('graph-result').innerHTML=`<p><b>${esc(g.origin)} → ${esc(g.destination)}</b> · green confirmed, orange attempted, grey observed</p><svg viewBox='0 0 ${w} ${h}' width='100%' height='auto' preserveAspectRatio='xMidYMin meet' role='img' aria-label='Message route graph'><defs><marker id='arrow' markerWidth='8' markerHeight='8' refX='6' refY='3' orient='auto'><path d='M0,0 L0,6 L7,3 z' fill='#555'></path></marker></defs>${edges}${circles}</svg>`}catch(e){document.getElementById('graph-result').textContent=e}};
// Semantic message-route graph: colour operation outcomes independently of
// passive RF evidence and annotate the actual local hop, retry, and route.
function messageGraphStyle(kind){return ({delivered:{color:'#16a34a',width:4,dash:'',name:'delivered'},pending:{color:'#f59e0b',width:3,dash:'8 5',name:'pending'},failed:{color:'#dc2626',width:3,dash:'',name:'failed'},reported:{color:'#2563eb',width:2.5,dash:'4 4',name:'reported'},observed:{color:'#94a3b8',width:2,dash:'',name:'observed'}}[kind]||{color:'#94a3b8',width:2,dash:'',name:'observed'})}
function semanticGraphEdgeLabel(edge){let label=edge.label||messageGraphStyle(edge.kind).name;if(edge.attempt_number)label+=` #${edge.attempt_number}`;if(edge.snr!=null&&!String(label).includes('dB')){let n=Number(edge.snr);label+=` ${Number.isFinite(n)?(Number.isInteger(n)?n:n.toFixed(1)):'?'} dB`}return label}
function semanticGraphLabelPlacement(edge,index,pos,occupied){let a=pos[edge.from],b=pos[edge.to],dx=b.x-a.x,dy=b.y-a.y,length=Math.max(1,Math.hypot(dx,dy)),nx=-dy/length,ny=dx/length,label=semanticGraphEdgeLabel(edge),width=Math.max(78,Math.min(190,label.length*6.5+16)),height=21,ts=[.22,.34,.46,.58,.70,.82];for(let k=0;k<120;k++){let t=ts[k%ts.length],ring=Math.floor(k/ts.length),offset=ring===0?0:(ring%2?-1:1)*(24+Math.ceil(ring/2)*24),x=a.x+dx*t+nx*offset,y=a.y+dy*t+ny*offset,box={left:x-width/2-4,right:x+width/2+4,top:y-16,bottom:y+8};if(!occupied.some(other=>graphBoxesOverlap(box,other)))return {x,y,width,height,box}}let x=a.x+dx*.5+nx*((index+1)*180),y=a.y+dy*.5+ny*((index+1)*180),box={left:x-width/2-4,right:x+width/2+4,top:y-16,bottom:y+8};return {x,y,width,height,box}}
showGraph=async id=>{try{let s=await api('/api/status'),g=await api('/api/graph?message_id='+encodeURIComponent(id)+'&origin='+encodeURIComponent(s.callsign||'')),nodes=g.nodes||[],edgesData=g.edges||[],paths=g.paths||[],displayPath=paths.filter(x=>Array.isArray(x.path)&&x.path.length>1).reduce((best,x)=>x.path.length>best.length?x.path:best,[]),hopByNode=Object.fromEntries(displayPath.map((node,index)=>[node,index+1])),radiusByNode=Object.fromEntries(nodes.map(node=>[node,graphNodeRadius(node)])),maxRadius=Math.max(32,...nodes.map(node=>radiusByNode[node])),cols=Math.min(4,Math.max(1,nodes.length)),rows=Math.max(1,Math.ceil(nodes.length/cols)),gapX=290,gapY=155,padX=maxRadius+30,padY=maxRadius+34,w=Math.max(760,padX*2+(cols-1)*gapX),h=Math.max(220,padY*2+(rows-1)*gapY),pos={};nodes.forEach((node,index)=>pos[node]={x:padX+(index%cols)*gapX,y:padY+Math.floor(index/cols)*gapY});let occupied=nodes.map(node=>{let r=radiusByNode[node],p=pos[node];return {left:p.x-r-9,right:p.x+r+9,top:p.y-r-9,bottom:p.y+r+9}}),edgeLines=[],edgeLabels=[];edgesData.forEach((edge,index)=>{let a=pos[edge.from],b=pos[edge.to];if(!a||!b)return;let style=messageGraphStyle(edge.kind),placement=semanticGraphLabelPlacement(edge,index,pos,occupied);occupied.push(placement.box);edgeLines.push(`<line x1='${a.x}' y1='${a.y}' x2='${b.x}' y2='${b.y}' stroke='${style.color}' stroke-width='${style.width}'${style.dash?` stroke-dasharray='${style.dash}'`:''} data-edge-kind='${esc(edge.kind)}' marker-end='url(#arrow)'></line>`);edgeLabels.push(`<g class='graph-edge-label'><rect x='${placement.x-placement.width/2}' y='${placement.y-15}' width='${placement.width}' height='${placement.height}' rx='5' fill='#fff' stroke='${style.color}' stroke-width='.8' opacity='.96'></rect><text x='${placement.x}' y='${placement.y}' text-anchor='middle' dominant-baseline='middle' font-size='12' fill='#18222d'>${esc(semanticGraphEdgeLabel(edge))}</text></g>`)});let incidentKind=node=>edgesData.filter(edge=>edge.from===node||edge.to===node).map(edge=>edge.kind).sort((a,b)=>({observed:0,reported:1,pending:2,failed:3,delivered:4}[b]??0)-({observed:0,reported:1,pending:2,failed:3,delivered:4}[a]??0))[0]||'observed',circles=nodes.map(node=>{let p=pos[node],r=radiusByNode[node],kind=incidentKind(node),style=messageGraphStyle(kind),fill=node===g.origin?'#1769aa':node===g.destination?'#a33':'#e8edf2',textFill=node===g.origin||node===g.destination?'white':'#18222d',hop=hopByNode[node];return `<circle cx='${p.x}' cy='${p.y}' r='${r}' fill='${fill}' stroke='${style.color}' stroke-width='${kind==='observed'?2:5}'></circle><text x='${p.x}' y='${p.y+4}' text-anchor='middle' font-size='12' fill='${textFill}'>${esc(node)}</text>${hop?`<circle cx='${p.x+r-5}' cy='${p.y-r+5}' r='11' fill='#18222d' stroke='#fff' stroke-width='1.5'></circle><text x='${p.x+r-5}' y='${p.y-r+9}' text-anchor='middle' font-size='11' font-weight='bold' fill='#fff'>${hop}</text>`:''}`}).join(''),pathSummary=paths.slice(-8).map(x=>{let p=(x.path||[]).map((node,i)=>`${i+1} ${node}`).join(' → ');return `<tr><td><span class='pill'>${esc(x.kind||'route')}</span></td><td>${esc(x.operation||'route')}</td><td>${x.attempt?esc(x.attempt):'—'}</td><td class='mono'>${esc(p)}</td></tr>`}).join('');document.getElementById('graph-result').innerHTML=`<p><b>${esc(g.origin)} → ${esc(g.destination)}</b> · <span style='color:#16a34a'>green delivered</span> · <span style='color:#f59e0b'>orange pending</span> · <span style='color:#dc2626'>red failed</span> · <span style='color:#2563eb'>blue reported</span> · grey observed</p><svg viewBox='0 0 ${w} ${h}' width='100%' height='auto' preserveAspectRatio='xMidYMin meet' role='img' aria-label='Semantic message route graph'><defs><marker id='arrow' markerWidth='8' markerHeight='8' refX='6' refY='3' orient='auto'><path d='M0,0 L0,6 L7,3 z' fill='#555'></path></marker></defs>${edgeLines.join('')}${edgeLabels.join('')}${circles}</svg>${pathSummary?`<details class='graph-path-summary'><summary>Latest routes and attempts</summary><table style='width:100%;table-layout:fixed'><thead><tr><th>Result</th><th>Operation</th><th>Attempt</th><th>Path</th></tr></thead><tbody>${pathSummary}</tbody></table></details>`:''}`}catch(e){document.getElementById('graph-result').textContent=e}};
function curveLiveGraphEdges(svg){return}
function markLiveGraphNodes(){let svg=document.querySelector('#live-graph svg');if(!svg)return;let links=[...svg.querySelectorAll('line')];svg.querySelectorAll('circle').forEach(circle=>{let x=Number(circle.getAttribute('cx')),y=Number(circle.getAttribute('cy')),kinds=links.filter(line=>[ ['x1','y1'],['x2','y2'] ].some(([px,py])=>Number(line.getAttribute(px))===x&&Number(line.getAttribute(py))===y)).map(line=>line.getAttribute('data-edge-kind')),kind=kinds.includes('reciprocal')?'confirmed':kinds.includes('active_one_way')?'attempted':'observed',colors={confirmed:['#16a34a','#d8f3dc'],attempted:['#f59e0b','#fff1c2'],observed:['#cbd5e1','#e8edf2']},color=colors[kind];circle.setAttribute('stroke',color[0]);circle.setAttribute('stroke-width',kind==='observed'?'2':'4');circle.setAttribute('fill',color[1])})}new MutationObserver(markLiveGraphNodes).observe(document.getElementById('live-graph'),{childList:true,subtree:true});
function scrollToCompose(){let panel=document.querySelector('.compose-panel');if(!panel)return;let header=document.querySelector('.topbar'),offset=(header?.getBoundingClientRect().height||0)+16;window.scrollTo({top:Math.max(0,window.scrollY+panel.getBoundingClientRect().top-offset),behavior:'smooth'})}
function useStation(call){document.querySelector('#compose input[name=destination]').value=call;scrollToCompose();setTimeout(()=>document.querySelector('#compose input[name=destination]')?.focus(),350)}
let stationCache=[];function renderStations(){let q=document.getElementById('station-search').value.trim().toUpperCase();let s=stationCache.filter(x=>!q||x.callsign.includes(q)||x.evidence.join(' ').toUpperCase().includes(q));document.getElementById('stations').innerHTML=s.length?'<table style="table-layout:fixed;width:100%"><colgroup><col style="width:18%"><col style="width:18%"><col style="width:14%"><col style="width:18%"><col style="width:18%"><col style="width:14%"></colgroup><tr><th style="white-space:nowrap">Callsign</th><th style="white-space:nowrap">Age</th><th style="white-space:nowrap">SNR</th><th>Path</th><th style="white-space:nowrap">Action</th><th title="JS8Mail capability" style="white-space:nowrap;text-align:center">JS8M</th></tr>'+s.map(x=>`<tr><td style="white-space:nowrap"><b>${esc(x.callsign)}</b></td><td style="white-space:nowrap">${esc(relativeAge(x.age_seconds))}</td><td style="white-space:nowrap">${x.snr==null?'—':esc(x.snr)+' dB'}</td><td style="white-space:normal">${[...new Set(x.evidence.map(evidenceLabel))].map(esc).join('<br>')}</td><td style="white-space:nowrap"><button style="white-space:nowrap" onclick="useStation('${esc(x.callsign)}')">Compose</button></td><td title="${x.js8m?'JS8Mail capable':'Not identified as JS8Mail capable'}" style="white-space:nowrap;text-align:center;padding-left:.5em;padding-right:.5em;color:#16a34a;font-size:1.15em">${x.js8m?'●':''}</td></tr>`).join('')+'</table>':'<p>No matching station evidence.</p>'}async function refreshStations(){stationCache=await api('/api/stations');renderStations()}
function renderInbox(items){document.getElementById('inbox').innerHTML=items.length?'<table><tr><th>From</th><th>Status</th><th>Message</th><th>Updated</th></tr>'+items.map(x=>`<tr><td><b>${esc(x.sender)}</b></td><td><span class='pill ${x.complete?'ok':'warn'}'>${x.complete?'Complete':'Partial · '+x.received_parts.length+'/'+x.total_parts+' parts'}</span></td><td class=mono>${esc(x.body)}</td><td>${esc(new Date(x.updated_at_ms).toLocaleString())}<br>${esc(x.path||'')}</td></tr>`).join('')+'</table>':'<p>No received messages.</p>'}
function renderControlEvents(items){let el=document.getElementById('control-events');if(!el)return;el.innerHTML=items.length?`<details class='control-events'><summary>Automatic delivery confirmations (${items.length})</summary><div class=mono>${items.map(x=>`<div><span class='timeline-time'>${new Date(x.created_at_ms).toLocaleTimeString()}</span> <b>${esc(x.label||'Automatic delivery update')}</b> → ${esc(x.target||'unknown')}: <span class='pill ${x.status==='submitted'?'ok':x.status==='failed'?'bad':'warn'}'>${esc(x.status||'waiting')}</span><br>${esc(x.detail||'')}${x.path?`<br>Path: ${esc(x.path)}`:''}</div>`).join('')}</div></details>`:''}
async function refreshControlEvents(){try{renderControlEvents(await api('/api/control-events'))}catch(_error){}}
refreshControlEvents();setInterval(refreshControlEvents,3000);
function ensureStoredRemoveButtons(){document.querySelectorAll('#messages tr').forEach(row=>{let first=row.cells?.[0],actions=row.cells?.[3];if(!first||!actions||!first.textContent.includes('Stored')||actions.querySelector('[data-stored-remove]'))return;let id=first.querySelector('details')?.dataset.id;if(!id)return;let button=document.createElement('button');button.className='danger';button.dataset.storedRemove='true';button.textContent='Remove';button.onclick=()=>act(id,'delete',button);actions.appendChild(button)})}
new MutationObserver(ensureStoredRemoveButtons).observe(document.getElementById('messages'),{childList:true,subtree:true});
ensureStoredRemoveButtons();
async function refresh(){let s=await api('/api/status'),statusHtml=`<span class='pill ${s.connected?'ok':'warn'}'>JS8Call: ${s.connected?'connected':'offline'}</span><span class=pill>Station: ${esc(s.callsign||'unknown')}</span><span class='pill ${s.paused?'warn':'ok'}'>RF: ${s.paused?'paused':'active'}</span><span class=pill>Port: ${s.port}</span><button onclick="togglePause()">${s.paused?'Resume RF':'Pause RF'}</button>`;let defaultMode=document.getElementById('default-enhanced-mode');if(defaultMode&&defaultMode.value!==s.enhanced_mode)defaultMode.value=s.enhanced_mode||'opportunistic';let statusEl=document.getElementById('status');if(statusEl.dataset.rendered!==statusHtml){statusEl.innerHTML=statusHtml;statusEl.dataset.rendered=statusHtml}let m=await api('/api/messages');let openIds=[...document.querySelectorAll('#messages details[open]')].map(d=>d.dataset.id);document.getElementById('messages').innerHTML=m.length?'<table><tr><th>Message</th><th>To</th><th>Content</th><th>Action</th></tr>'+m.map(x=>`<tr><td><details data-id='${esc(x.id)}' ${openIds.includes(x.id)?'open':''}><summary>${statePill(x)} · <span class=pill>${esc(confidenceName[x.confidence]||confidenceName.uncertain)}</span><br><small>${esc(x.id)}</small>${x.next_attempt_at_ms?` · retry ${new Date(x.next_attempt_at_ms).toLocaleTimeString()} (#${x.retry_count})`:''}</summary><div class=mono>${(x.attempts||[]).map(a=>`<span class=timeline-time>${new Date(a.created_at_ms).toLocaleTimeString()}</span> ${esc(a.action)} → ${esc(a.target)}: ${esc(a.status)}${a.detail?' · '+esc(formatAttemptDetail(a.detail)):''}`).join('<br>')||'No attempts recorded.'}</div><div class='message-full outbox-full-content'><b>Full content</b><br>${esc(x.body)}</div></details></td><td>${esc(x.destination)}</td><td data-preview='true'><div class=message-preview><b>${esc(x.subject||'(no subject)')}</b><br>${esc(x.body)}</div></td><td><button onclick="showGraph('${x.id}')">Graph</button>${['queued','waiting_route','in_progress','acknowledged'].includes(x.state)?`<button onclick="act('${x.id}','retry-now')">Retry now</button>`:''}${['queued','waiting_route','in_progress'].includes(x.state)?`<button class=danger onclick="act('${x.id}','cancel')">Cancel</button>`:''}${['failed','cancelled','expired','delivered','acknowledged'].includes(x.state)?`<button class=danger onclick="act('${x.id}','delete')">Remove</button>`:''}</td></tr>`).join('')+'</table>':'<p>No messages.</p>';let o=await api('/api/observations');document.getElementById('observations').innerHTML=o.map(x=>`<div class=mono>${new Date(x.observed_at_ms).toLocaleTimeString()} ${esc(x.event_type)} ${esc(x.value)}</div>`).join('')||'<p>Waiting for JS8Call events.</p>'}
const EMERGENCY_GROUPS=['@JS8MAIL','@EMCOMM','@ARES','@RACES','@RAYNET','@NTS','@SKYWARN','@WX','@AMRRON'];function useGroup(group){document.querySelector('#compose input[name=destination]').value=group;document.querySelector('#compose input[name=destination]').focus()}function renderGroups(items){let groups=items.filter(x=>x.name!=='@HB'&&EMERGENCY_GROUPS.includes(x.name));document.getElementById('groups').innerHTML=groups.length?'<table><tr><th>Group</th><th>Purpose</th><th>Seen</th><th>Subscription</th><th>Action</th></tr>'+groups.map(x=>`<tr class="${x.subscribed?'group-subscribed':'group-unsubscribed'}"><td><b>${esc(x.name)}</b></td><td>${esc(x.description||'emergency group')}</td><td>${x.seen_count?esc(relativeAge((Date.now()-x.last_seen_at_ms)/1000)):'not yet observed'}</td><td><span class="pill group-subscription ${x.subscribed?'subscribed':'unsubscribed'}">${x.subscribed?'Subscribed':'Not subscribed'}</span></td><td><button onclick="useGroup('${esc(x.name)}')">Compose</button><button onclick="actGroup('${esc(x.name)}','${x.subscribed?'unsubscribe':'subscribe'}')">${x.subscribed?'Unsubscribe':'Subscribe'}</button></td></tr>`).join('')+'</table>':'<p>No emergency groups recorded.</p>'}function renderAlerts(items){let alerts=items.filter(x=>x.group_name);window.groupAlertItems=alerts;document.getElementById('alerts').innerHTML=alerts.length?alerts.map((x,i)=>`<article><b>${esc(x.group_name)} · ${esc(x.sender)}</b> <span class='pill ${x.complete?'ok':'warn'}'>${x.complete?'Complete':'Partial · '+x.received_parts.length+'/'+x.total_parts}</span><div class=mono>${esc(x.body)}</div><small>${esc(new Date(x.updated_at_ms).toLocaleString())} · ${esc(x.path||'')}</small><br><button class=danger onclick='deleteInboxMessage(groupAlertItems[${i}])'>Remove</button></article>`).join(''):'<p>No group alerts received.</p>'}
const refreshMailbox=refresh;refresh=async()=>{let inbox=await api('/api/inbox');renderInbox(inbox);renderAlerts(inbox);let groups=await api('/api/groups');renderGroups(groups);return refreshMailbox()};const updateRadioLeds=async()=>{let s=await api('/api/status'),activity=s.connected?(s.radio_activity||'RX'):'ERR';document.querySelectorAll('#radio-leds .led').forEach(x=>x.className='led');let led=document.getElementById('led-'+activity.toLowerCase());if(led)led.className='led on-'+activity.toLowerCase()};const refreshWithRadioState=refresh;refresh=async()=>{await refreshWithRadioState();await updateRadioLeds()};
async function actGroup(group,action){try{await api(`/api/groups/${encodeURIComponent(group)}/${action}`,{method:'POST'});refresh()}catch(e){alert(e)}}
function syncOutboxHistory(){if(!document.getElementById('outbox-history-style')){let style=document.createElement('style');style.id='outbox-history-style';style.textContent='#messages details > .mono,#messages details > .message-full{display:none!important}.outbox-history-row td{background:#f7f9fb;padding:.65em .5em}.outbox-history-row .message-full{margin-top:.6em}';document.head.appendChild(style)}document.querySelectorAll('#messages details[data-id]').forEach(detail=>{let row=detail.closest('tr');if(!row)return;let next=row.nextElementSibling;if(detail.open){if(!next||!next.classList.contains('outbox-history-row')||next.dataset.forId!==detail.dataset.id){if(next&&next.classList.contains('outbox-history-row'))next.remove();let history=document.createElement('tr');history.className='outbox-history-row';history.dataset.forId=detail.dataset.id;let cell=document.createElement('td');cell.colSpan=4;let timeline=detail.querySelector(':scope > .mono');let full=detail.querySelector(':scope > .message-full');if(timeline){let copy=timeline.cloneNode(true);copy.style.display='block';cell.append(copy)}if(full){let copy=full.cloneNode(true);copy.style.display='block';cell.append(copy)}history.append(cell);row.after(history)}}else if(next&&next.classList.contains('outbox-history-row'))next.remove()})}
document.addEventListener('toggle',e=>{if(e.target.matches?.('#messages details'))setTimeout(syncOutboxHistory,0)},true);syncOutboxHistory();setInterval(syncOutboxHistory,1000);
async function deleteInboxMessage(item){if(!item||!confirm('Delete this local inbox message?'))return;try{await api(`/api/inbox/${encodeURIComponent(item.sender)}/${encodeURIComponent(item.message_id)}/delete`,{method:'POST'});refresh()}catch(e){alert(e)}}
async function act(id,a,button){if(button){button.disabled=true}try{await api(`/api/messages/${encodeURIComponent(id)}/${a}`,{method:'POST'});await refresh()}catch(e){if(button){button.disabled=false}await refresh().catch(()=>{});let message=e instanceof Error?e.message:String(e);let event=document.getElementById('control-events');if(event){event.textContent=`Action failed: ${message}`;event.className='error'}else{alert(message)}}}
async function togglePause(){try{let s=await api('/api/status');await api(`/api/control/${s.paused?'resume':'pause'}`,{method:'POST'});refresh()}catch(e){alert(e)}}
const expandedMessages=new Set;document.addEventListener('click',e=>{let summary=e.target.closest?.('#messages details summary');if(!summary)return;setTimeout(()=>{let detail=summary.parentElement,id=detail?.querySelector('small')?.textContent.trim();if(id){if(detail.open)expandedMessages.add(id);else expandedMessages.delete(id)}},0)});function collapseExpandedOutbox(){expandedMessages.clear();document.querySelectorAll('#messages details[open]').forEach(detail=>{detail.open=false})}function restoreExpanded(){document.querySelectorAll('#messages details').forEach(d=>{let id=d.querySelector('small')?.textContent.trim();if(id&&expandedMessages.has(id))d.open=true})}const refreshKeepExpanded=refresh;refresh=async()=>{if(document.querySelector('#messages details[open]')){await updateRadioLeds();return}await refreshKeepExpanded();restoreExpanded()};
const refreshStable=async()=>{let inbox=await api('/api/inbox');renderInbox(inbox);renderAlerts(inbox);let groups=await api('/api/groups');renderGroups(groups);await refreshMailbox()};refresh=async()=>{await refreshStable();await updateRadioLeds();restoreExpanded()};async function addMessageControls(){if(!document.getElementById('outbox-layout-style')){let style=document.createElement('style');style.id='outbox-layout-style';style.textContent='#messages th:nth-child(3),#messages td:nth-child(3){width:15em}#messages th:nth-child(4),#messages td:nth-child(4){width:21em;white-space:normal}#messages td:nth-child(4) button{margin:0;padding:.4em .55em;white-space:nowrap}@media(max-width:800px){#messages th:nth-child(3),#messages td:nth-child(3),#messages th:nth-child(4),#messages td:nth-child(4){width:auto}}';document.head.appendChild(style)}document.querySelectorAll('#messages tr').forEach(row=>{let content=row.cells?.[2],details=row.querySelector('details');if(!content||content.dataset.preview)return;let full=content.innerHTML;content.dataset.preview='true';content.innerHTML=`<div class=message-preview>${full}</div>`;if(details){let expanded=document.createElement('div');expanded.className='message-full outbox-full-content';expanded.innerHTML=`<b>Full content</b><br>${full}`;details.appendChild(expanded)}})}
document.getElementById('compose').onsubmit=async e=>{e.preventDefault();try{let x=await api('/api/messages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});document.getElementById('result').textContent='Queued '+x.id;e.target.reset();collapseExpandedOutbox();await refresh();document.getElementById('messages')?.closest('section')?.scrollIntoView({behavior:'smooth',block:'start'})}catch(e){document.getElementById('result').textContent=e}}
document.getElementById('default-enhanced-mode').onchange=async e=>{try{await api('/api/settings/enhanced-mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:e.target.value})});document.getElementById('result').textContent='Default JS8M mode saved'}catch(err){document.getElementById('result').textContent=err}}
document.addEventListener('submit',e=>{if(e.target.id==='compose')collapseExpandedOutbox()},true);
document.getElementById('station-search').oninput=renderStations;
const destinationInput=document.querySelector('#compose input[name="destination"]');
const enhancedModeInput=document.querySelector('#compose select[name="enhanced_mode"]');
const destinationCapability=document.createElement('small');
destinationCapability.id='destination-capability';
destinationCapability.style.display='block';
destinationInput?.after(destinationCapability);
let capabilityLookup=0;
async function updateDestinationCapability(){
  let callsign=destinationInput?.value.trim().toUpperCase()||'';
  let lookup=++capabilityLookup;
  destinationCapability.textContent='';
  if(!callsign||callsign.startsWith('@'))return;
  try{
    let result=await api('/api/capability?callsign='+encodeURIComponent(callsign));
    if(lookup!==capabilityLookup||!result.js8m)return;
    destinationCapability.innerHTML=' <span style="color:#16a34a;font-weight:600">● JS8M capable</span> · Opportunistic mode recommended';
    if(enhancedModeInput?.value!=='opportunistic'){
      let button=document.createElement('button');
      button.type='button'; button.textContent='Use Opportunistic';
      button.onclick=()=>{enhancedModeInput.value='opportunistic';updateDestinationCapability()};
      destinationCapability.append(' ',button);
    }
  }catch(_error){if(lookup===capabilityLookup)destinationCapability.textContent=''}
}
destinationInput?.addEventListener('input',updateDestinationCapability);
enhancedModeInput?.addEventListener('change',updateDestinationCapability);
function showInboxMessage(item){let modal=document.getElementById('message-modal');if(!modal){modal=document.createElement('div');modal.id='message-modal';modal.innerHTML='<div class="modal-card" role="dialog" aria-modal="true"><button class="danger modal-close" onclick="closeInboxMessage()">Close</button><div id="message-modal-content"></div></div>';document.body.appendChild(modal)}let delivery={direct:'Direct',forwarded:'Forwarded',stored_collected:'Stored → collected',group_broadcast:'Group broadcast'}[item.delivery]||'Direct';document.getElementById('message-modal-content').innerHTML=`<h2>${esc(item.subject||'(no subject)')}</h2><p><b>From:</b> ${esc(item.sender)} · <b>Status:</b> ${item.complete?'Complete':'Partial · '+item.received_parts.length+'/'+item.total_parts+' parts'} · <b>Protocol:</b> ${item.protocol==='js8m'?'JS8Mail':'Standard'}</p><p><b>Delivery:</b> ${esc(delivery)}<br><b>Path:</b> ${esc(item.path||item.sender||'Unknown')}</p><div class=message-full>${esc(item.body)}</div><p><small>Received ${esc(new Date(item.updated_at_ms).toLocaleString())}</small></p><button onclick='replyToInboxMessage(inboxItems[${window.inboxItems?.indexOf(item)??-1}])'>Reply</button>`;modal.style.display='flex'}function closeInboxMessage(){let modal=document.getElementById('message-modal');if(modal)modal.style.display='none'}function replyToInboxMessage(item){if(!item)return;closeInboxMessage();let destination=document.querySelector('#compose input[name=destination]'),subject=document.querySelector('#compose input[name=subject]'),body=document.querySelector('#compose textarea[name=body]');destination.value=item.sender;subject.value=item.subject?('Re: '+item.subject).slice(0,120):'';body.focus();document.querySelector('.compose-panel')?.scrollIntoView({behavior:'smooth',block:'start'})}
const inboxRender=renderInbox;renderInbox=items=>{document.getElementById('inbox').innerHTML=items.length?'<table><tr><th>From</th><th>Status</th><th>Message</th><th>Action</th></tr>'+items.map((x,i)=>`<tr><td><b>${esc(x.sender)}</b></td><td><span class='pill ${x.complete?'ok':'warn'}'>${x.complete?'Complete':'Partial · '+x.received_parts.length+'/'+x.total_parts}</span><br><span class='pill ${x.protocol==='js8m'?'enhanced':''}'>${x.protocol==='js8m'?'JS8Mail':'Standard'}</span></td><td><div class=message-preview>${esc(x.body)}</div></td><td><button onclick='showInboxMessage(inboxItems[${i}])'>Open</button><button class=danger onclick='deleteInboxMessage(inboxItems[${i}])'>Delete</button></td></tr>`).join('')+'</table>':'<p>No received messages.</p>';window.inboxItems=items};
// Marking an inbox item read is durable, while this wrapper keeps the
// existing modal renderer and refresh behaviour unchanged.
const existingShowInboxMessage=showInboxMessage;showInboxMessage=async item=>{if(!item)return;try{await api(`/api/inbox/${encodeURIComponent(item.sender)}/${encodeURIComponent(item.message_id)}/read`,{method:'POST'});item.is_read=true;renderInbox(window.inboxItems||[])}catch(_error){}return existingShowInboxMessage(item)};
// Make the protocol classification prominent in the full-message view while
// retaining the compact table layout.
const showInboxMessageWithProtocol=showInboxMessage;showInboxMessage=async item=>{await showInboxMessageWithProtocol(item);let p=[...document.querySelectorAll('#message-modal-content p')].find(x=>x.textContent.includes('Protocol:'));if(p&&item?.protocol==='js8m'&&!p.querySelector('.enhanced'))p.innerHTML=p.innerHTML.replace('JS8Mail','<span class="pill enhanced">JS8Mail · J8M1 v1</span>')};
const existingInboxRenderer=renderInbox;renderInbox=items=>{existingInboxRenderer(items);document.querySelectorAll('#inbox tr.inbox-new').forEach(row=>row.classList.remove('inbox-new'));(window.inboxItems||[]).forEach((item,index)=>{if(!item.is_read){let row=document.querySelectorAll('#inbox tr')[index+1];if(row)row.classList.add('inbox-new')}})};
let modalStyle=document.createElement('style');modalStyle.textContent='#message-modal,#version-modal{display:none;position:fixed;inset:0;background:#18222d88;z-index:20;align-items:center;justify-content:center;padding:1em}.modal-card{background:white;border-radius:10px;box-shadow:0 8px 30px #18222d66;max-width:720px;width:min(720px,100%);max-height:85vh;overflow:auto;padding:1.2em}.modal-close{float:right}.message-preview{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;white-space:pre-wrap}.message-full{white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid #d9e0e7;border-radius:6px;padding:1em;background:#f7f9fb}.inbox-new{background:#eff6ff}.timeline-time{color:#64748b;font-variant-numeric:tabular-nums}.pill.good,.pill.enhanced{background:#b7f0d0;color:#075c38}.pill.bad{background:#ffd9d9;color:#8b1e1e}.live-legend{font-weight:700;white-space:nowrap}.live-legend-reciprocal{color:#16a34a}.live-legend-active{color:#f59e0b}.live-legend-aged{color:#64748b}.live-legend-js8m{color:#0f766e;font-weight:700}';document.head.appendChild(modalStyle);
let outboxPreviewStyle=document.createElement('style');outboxPreviewStyle.textContent='#messages td:nth-child(3){height:5.5em;max-height:5.5em;overflow:hidden;line-height:1.25}#messages td:nth-child(3) .message-preview{max-height:4.5em}.confidence-stored_at_custodian,.confidence-delivered_to_js8mail{background:#b7f0d0;color:#075c38}.confidence-awaiting_delivery_ack,.confidence-awaiting_custodian_ack,.confidence-enhanced_acknowledged,.confidence-enhanced_acknowledged_stopped{background:#fff1c2;color:#7a4b00}.confidence-delivery_uncertain{background:#ffd9d9;color:#8b1e1e}.confidence-radio_acknowledged{background:#dbeafe;color:#174ea6}';document.head.appendChild(outboxPreviewStyle);
let groupSubscriptionStyle=document.createElement('style');groupSubscriptionStyle.textContent='.group-subscribed{background:#f0fdf4}.group-unsubscribed{background:#fff}.group-subscription{font-size:.82em;white-space:nowrap}.group-subscription.subscribed{background:#bbf7d0;color:#166534}.group-subscription.unsubscribed{background:#e5e7eb;color:#4b5563}';document.head.appendChild(groupSubscriptionStyle);
function styleOutboxConfidence(){document.querySelectorAll('#messages details summary .pill').forEach(p=>{let text=p.textContent||'',key=text.startsWith('Delivered to custodian')?'stored_at_custodian':text.startsWith('Delivered to JS8Mail')?'delivered_to_js8mail':text.startsWith('Store offer submitted')?'awaiting_custodian_ack':text.startsWith('TX submitted')?'awaiting_delivery_ack':text.startsWith('Delivery unconfirmed')?'delivery_uncertain':text.startsWith('JS8Call accepted')?'enhanced_acknowledged_stopped':text.startsWith('JS8Call ACK')?'enhanced_acknowledged':text==='Standard'?'radio_acknowledged':'';if(key)p.classList.add('confidence-'+key)})}new MutationObserver(styleOutboxConfidence).observe(document.getElementById('messages'),{childList:true,subtree:true});
function renderLiveGraph(g){let box=document.getElementById('live-graph');if(!box)return;if(!g.nodes.length){box.innerHTML='<p>Waiting for active-band observations.</p>';return}let cols=Math.min(6,Math.max(2,Math.ceil(Math.sqrt(g.nodes.length)))),rows=Math.ceil(g.nodes.length/cols),w=Math.max(720,cols*150+80),h=Math.max(280,rows*90+70),pos={};g.nodes.forEach((n,i)=>pos[n]={x:50+(i%cols)*150,y:45+Math.floor(i/cols)*90});let edges=g.edges.map(e=>{let a=pos[e.from],b=pos[e.to],kind=e.kind==='reciprocal'?'reciprocal':e.kind==='active_one_way'?'active_one_way':'aged',color=kind==='reciprocal'?'#16a34a':kind==='active_one_way'?'#f59e0b':'#cbd5e1',width=e.js8m?5:kind==='reciprocal'?4:kind==='active_one_way'?3:2;return `<line data-edge-kind='${kind}' x1=${a.x} y1=${a.y} x2=${b.x} y2=${b.y} stroke='${color}' stroke-opacity='1' opacity='1' stroke-width='${width}'/>`}).join('');let circles=g.nodes.map(n=>`<g><circle cx=${pos[n].x} cy=${pos[n].y} r=25 fill='#e8edf2' stroke='#18222d'/><text x=${pos[n].x} y=${pos[n].y+4} text-anchor=middle font-size=11>${esc(n)}</text></g>`).join('');box.innerHTML=`<p class=mono>Band ${esc(g.band)} · ${g.nodes.length} stations · ${g.edges.length} links · last 2 hours</p><svg viewBox='0 0 ${w} ${h}' preserveAspectRatio='xMidYMin meet' role='img' aria-label='Live radio activity graph'>${edges}${circles}</svg><small><span class='live-legend live-legend-reciprocal'>Reciprocal</span> · <span class='live-legend live-legend-active'>Active one-way</span> · <span class='live-legend live-legend-aged'>Aged one-way</span><br><span class='live-legend live-legend-js8m'>Thick links = JS8Mail evidence</span></small>`}async function refreshLiveGraph(){try{let g=await api('/api/live-graph');window.liveGraphData=g;renderLiveGraph(g)}catch(e){document.getElementById('live-graph').textContent='Live graph unavailable'}}refreshLiveGraph();setInterval(refreshLiveGraph,3000);
function compactOutboxTimelines(){document.querySelectorAll('#messages details .mono').forEach(el=>{if(el.dataset.compacted==='1')return;let raw=(el.innerHTML||'').split('<br>');if(raw.length<2)return;let entries=raw.map(html=>{let holder=document.createElement('span');holder.innerHTML=html;let time=holder.querySelector('.timeline-time')?.textContent||'';holder.querySelector('.timeline-time')?.remove();return {html,key:holder.textContent.replace(/\\s+/g,' ').trim(),time}}),rendered=[],i=0;while(i<entries.length){let bestLen=0,bestCount=0;for(let len=1;len<=Math.min(4,Math.floor((entries.length-i)/2));len++){let count=1;while(i+(count+1)*len<=entries.length&&entries.slice(i,i+len).every((item,j)=>item.key===entries[i+count*len+j].key))count++;if(count>=2&&count*len>bestCount*bestLen){bestLen=len;bestCount=count}}if(bestCount>=2){let block=entries.slice(i,i+bestLen),times=block.map(x=>x.time).filter(Boolean),summary=block.map(x=>x.key).join(' · ');rendered.push(`<span class='timeline-time'>${esc(times[0]||'')}</span> <span class='pill warn'>Repeated ×${bestCount}</span> ${esc(summary)}${times.length>1?` <span class='timeline-time'>through ${esc(times[times.length-1])}</span>`:''}`);i+=bestLen*bestCount}else{rendered.push(entries[i].html);i++}}if(rendered.length<raw.length){el.innerHTML=rendered.join('<br>');el.dataset.compacted='1'}})}let outboxTimelineObserver=new MutationObserver(compactOutboxTimelines);outboxTimelineObserver.observe(document.getElementById('messages'),{childList:true,subtree:true});compactOutboxTimelines();
function formatDial(hz){let n=Number(hz);return Number.isFinite(n)&&n>0?(n/1000000).toFixed(5)+' MHz':'not set'}
function applyLiveEdgeSemantics(){let g=window.liveGraphData,svg=document.querySelector('#live-graph svg');if(!g||!svg)return;let edges=[...svg.querySelectorAll('line, path')].filter(e=>e.tagName.toLowerCase()==='line'||!e.closest('defs'));edges.slice(0,g.edges.length).forEach((element,index)=>{let edge=g.edges[index],palette=edge.kind==='reciprocal'?['#16a34a','1',4]:edge.kind==='active_one_way'?['#f59e0b','1',3]:['#cbd5e1','.75',2],color=palette[0],opacity=palette[1],width=edge.js8m?5:palette[2];element.setAttribute('data-edge-kind',String(edge.kind));element.setAttribute('stroke',color);element.setAttribute('stroke-opacity',opacity);element.setAttribute('opacity','1');element.setAttribute('stroke-width',String(width))})}new MutationObserver(()=>applyLiveEdgeSemantics()).observe(document.getElementById('live-graph'),{childList:true,subtree:true});
function strengthenLiveGraphColors(){let svg=document.querySelector('#live-graph svg');if(!svg)return;svg.querySelectorAll('line').forEach(line=>{let stroke=line.getAttribute('stroke'),opacity=Number(line.getAttribute('stroke-opacity')||1);if(stroke==='#00a83b'){line.setAttribute('stroke','#00c853');line.setAttribute('stroke-opacity',String(Math.max(.75,opacity)))}else if(stroke==='#c77800'){line.setAttribute('stroke','#ff6d00');line.setAttribute('stroke-opacity',String(Math.max(.7,opacity)))}})}new MutationObserver(strengthenLiveGraphColors).observe(document.getElementById('live-graph'),{childList:true,subtree:true});
let liveLegendBreakObserver=new MutationObserver(()=>document.querySelectorAll('#live-graph small').forEach(s=>{if(s.innerHTML.includes(' · thick links carry JS8Mail'))s.innerHTML=s.innerHTML.replace(' · thick links carry JS8Mail','<br>Thick links carry JS8Mail')}));liveLegendBreakObserver.observe(document.getElementById('live-graph'),{childList:true,subtree:true});
let liveLegendObserver=new MutationObserver(()=>{let live=document.getElementById('live-graph');if(live&&live.innerHTML.includes('grey isolated one-way'))live.innerHTML=live.innerHTML.replace('grey isolated one-way','grey aged one-way')});liveLegendObserver.observe(document.getElementById('live-graph'),{childList:true,subtree:true});
// Live RF activity intentionally remains straight-line: unlike the message
// route graph it is a dense activity overview, and curved edges obscure which
// nodes are actually connected.
document.addEventListener('click',e=>{let button=e.target.closest?.('#messages button');if(button&&button.textContent.trim()==='Graph')setTimeout(()=>document.querySelector('#graph-result')?.closest('section')?.scrollIntoView({behavior:'smooth',block:'start'}),50)});
let actionGapStyle=document.createElement('style');actionGapStyle.textContent='#messages td:nth-child(4) button{margin-right:.45em!important;margin-bottom:.25em!important}#messages td:nth-child(4) button:last-child{margin-right:0!important}section{scroll-margin-top:8rem}';document.head.appendChild(actionGapStyle);
function styleStatusPills(){let bar=document.getElementById('status'),p=bar?.querySelectorAll('.pill');if(!p||p.length<5)return;let s=window.lastStatus||{};p[0].className='pill '+(s.connected?'good':'bad');p[1].className='pill '+(s.callsign?'good':'bad');p[2].className='pill '+(s.speed!==''&&s.speed!=='unknown'&&s.speed!=='unavailable'?'good':'bad');p[3].className='pill '+(s.tx_mode?'good':'bad');p[4].className='pill '+(s.connected?'good':'bad')}
async function updateBandStatus(){try{let s=await api('/api/status'),el=document.getElementById('status-band');window.lastStatus=s;if(!el){el=document.createElement('span');el.id='status-band';el.className='pill';document.getElementById('status').appendChild(el)}let valid=Boolean(s.band)&&Number(s.dial_frequency)>0;el.className='pill '+(valid?'good':'bad');let value=`Band: ${s.band||'not set'} · Dial: ${formatDial(s.dial_frequency)}`;if(el.textContent!==value)el.textContent=value;styleStatusPills()}catch(e){}}
document.head.insertAdjacentHTML('beforeend',"<style>#status .pill:nth-child(3){display:inline-block!important}</style>");updateBandStatus();setInterval(updateBandStatus,3000);
// refresh() redraws #status, so keep the band pill attached to the current
// status contents instead of allowing that redraw to remove it.
new MutationObserver(()=>updateBandStatus()).observe(document.getElementById('status'),{childList:true});
function movePauseControl(){let bar=document.getElementById('status'),band=document.getElementById('status-band'),button=bar?.querySelector('button');if(bar&&band&&button&&band.nextElementSibling!==button)band.after(button)}new MutationObserver(movePauseControl).observe(document.getElementById('status'),{childList:true});setInterval(movePauseControl,3000);movePauseControl();
async function updateProtocolLeds(){try{let s=await api('/api/status'),now=Date.now(),dcd=document.getElementById('led-dcd'),bar=document.getElementById('radio-leds'),js8=document.getElementById('led-js8');if(dcd)dcd.className='led'+(Number(s.dcd_until_ms||0)>now?' on-dcd':'');if(!js8&&bar){js8=document.createElement('span');js8.id='led-js8';js8.className='led';js8.textContent='JS8';bar.appendChild(js8)}if(js8)js8.className='led'+(Number(s.js8_activity_until_ms||0)>now?' on-js8':'')}catch(e){}}
let protocolLedStyle=document.createElement('style');protocolLedStyle.textContent='.led.on-js8{background:#d9d2ff;color:#4b2c82}';document.head.appendChild(protocolLedStyle);updateProtocolLeds();setInterval(updateProtocolLeds,1000);
const refreshWithoutOpenOutbox=async()=>{let inbox=await api('/api/inbox');renderInbox(inbox);renderAlerts(inbox);let groups=await api('/api/groups');renderGroups(groups);if(!document.querySelector('#messages details[open]'))await refreshMailbox();await updateRadioLeds();restoreExpanded()};refresh=refreshWithoutOpenOutbox;
const standardInboxRenderer=renderInbox;renderInbox=items=>standardInboxRenderer(items.filter(x=>!x.group_name));
const cataloguedGroupRenderer=renderGroups;renderGroups=items=>{let observed=items.filter(x=>Number(x.seen_count)>0).map(x=>x.name);let added=observed.filter(x=>!EMERGENCY_GROUPS.includes(x));EMERGENCY_GROUPS.push(...added);cataloguedGroupRenderer(items);EMERGENCY_GROUPS.splice(EMERGENCY_GROUPS.length-added.length,added.length)};
// Keep refreshes single-flight. A long API response must never start another
// full Outbox render on top of the previous one.
let mailboxRefreshInFlight=false,stationsRefreshInFlight=false;
async function refreshMailboxPage(){if(mailboxRefreshInFlight)return;mailboxRefreshInFlight=true;try{await refresh();await addMessageControls()}catch(e){let box=document.getElementById('messages');if(box&&box.textContent.trim()==='Loading…')box.textContent='Outbox temporarily unavailable; retrying…'}finally{mailboxRefreshInFlight=false}}
async function refreshStationsPage(){if(stationsRefreshInFlight)return;stationsRefreshInFlight=true;try{await refreshStations()}catch(_e){}finally{stationsRefreshInFlight=false}}
refreshMailboxPage();refreshStationsPage();setInterval(refreshMailboxPage,3000);setInterval(refreshStationsPage,5000);
// Apply the unread row decoration after the final mailbox renderer wrapper.
const finalInboxRenderer=renderInbox;renderInbox=items=>{finalInboxRenderer(items);let rows=[...document.querySelectorAll('#inbox tr')].slice(1);(window.inboxItems||[]).forEach((item,index)=>{if(!item.is_read&&rows[index])rows[index].classList.add('inbox-new')})};
const inboxBulkSelection=new Set;
function inboxKey(item){return `${item.sender}|${item.message_id}`}
function updateInboxBulkState(){let box=document.getElementById('inbox'),bar=document.getElementById('inbox-bulk-actions');if(!box||!bar)return;let items=window.inboxItems||[],count=items.filter(item=>inboxBulkSelection.has(inboxKey(item))).length;bar.querySelector('[data-bulk-count]').textContent=count?`${count} selected`:'Select messages';bar.querySelectorAll('button[data-bulk-action]').forEach(button=>button.disabled=!count);let selectAll=bar.querySelector('[data-select-all]');if(selectAll){selectAll.checked=Boolean(items.length&&count===items.length);selectAll.indeterminate=Boolean(count&&count<items.length)}box.querySelectorAll('[data-inbox-row]').forEach(row=>row.classList.toggle('inbox-row-selected',inboxBulkSelection.has(row.dataset.inboxRow)));box.querySelectorAll('[data-inbox-select]').forEach(input=>{input.checked=inboxBulkSelection.has(input.dataset.inboxKey)})}
function renderInboxModern(items){let box=document.getElementById('inbox');if(!box)return;window.inboxItems=items;let toolbar=`<div id='inbox-bulk-actions' class='inbox-bulk-bar'><label><input type='checkbox' data-select-all> Select all</label><span class='inbox-bulk-count' data-bulk-count>Select messages</span><button type='button' data-bulk-action data-bulk-read>Mark as read</button><button type='button' class='danger' data-bulk-action data-bulk-delete>Delete</button><span class='error' data-bulk-error></span></div>`;if(!items.length){box.innerHTML=toolbar+'<p>No received messages.</p>'}else{box.innerHTML=toolbar+`<table><tr><th class='inbox-select-cell' aria-label='Select'></th><th>From</th><th>Status</th><th>Message</th><th>Updated</th><th>Action</th></tr>${items.map((x,i)=>`<tr data-inbox-row='${esc(inboxKey(x))}' class='${x.is_read?'':'inbox-new'}'><td class='inbox-select-cell'><input type='checkbox' data-inbox-select data-inbox-key='${esc(inboxKey(x))}' aria-label='Select message from ${esc(x.sender)}'></td><td><b>${esc(x.sender)}</b></td><td><span class='pill ${x.complete?'ok':'warn'}'>${x.complete?'Complete':'Partial · '+x.received_parts.length+'/'+x.total_parts}</span><br><span class='pill ${x.protocol==='js8m'?'enhanced':''}'>${x.protocol==='js8m'?'JS8Mail':'Standard'}</span></td><td><div class='message-preview'><b>${esc(x.subject||'(no subject)')}</b><br>${esc(x.body)}</div></td><td>${esc(new Date(x.updated_at_ms).toLocaleString())}<br><span class=mono>${esc(x.path||'')}</span></td><td><button onclick='showInboxMessage(inboxItems[${i}])'>Open</button><button class=danger onclick='deleteInboxMessage(inboxItems[${i}])'>Delete</button></td></tr>`).join('')}</table>`}let selectAll=box.querySelector('[data-select-all]');selectAll.onchange=event=>{items.forEach(item=>{if(event.target.checked)inboxBulkSelection.add(inboxKey(item));else inboxBulkSelection.delete(inboxKey(item))});updateInboxBulkState()};box.querySelector('[data-bulk-read]').onclick=()=>bulkInboxAction('read');box.querySelector('[data-bulk-delete]').onclick=()=>bulkInboxAction('delete');box.querySelectorAll('[data-inbox-select]').forEach(input=>input.onchange=event=>{let key=event.target.dataset.inboxKey;if(event.target.checked)inboxBulkSelection.add(key);else inboxBulkSelection.delete(key);updateInboxBulkState()});updateInboxBulkState()}
async function bulkInboxAction(action){let items=(window.inboxItems||[]).filter(item=>inboxBulkSelection.has(inboxKey(item)));if(!items.length)return;if(action==='delete'&&!confirm(`Delete ${items.length} selected message${items.length===1?'':'s'}?`))return;let error=document.querySelector('[data-bulk-error]');if(error)error.textContent='';try{await Promise.all(items.map(item=>api(`/api/inbox/${encodeURIComponent(item.sender)}/${encodeURIComponent(item.message_id)}/${action}`,{method:'POST'})));inboxBulkSelection.clear();await refresh()}catch(e){if(error)error.textContent=e instanceof Error?e.message:String(e)}}
// Render the selection column and toolbar as one stable table. This avoids
// inserting checkboxes into a separately-rendered table after every refresh,
// which previously caused broken alignment and lost selection highlighting.
renderInbox=renderInboxModern;
// Keep the actual message protocol separate from current peer capability.
// A green dot means future Opportunistic delivery may use enhanced framing;
// it does not retroactively change a Standard message.
const renderInboxWithCapabilityHint=renderInbox;renderInbox=items=>{renderInboxWithCapabilityHint(items);let rows=[...document.querySelectorAll('#inbox tr[data-inbox-row]')];(items||[]).forEach((item,index)=>{if(item?.protocol!=='standard'||!item.peer_js8m||!rows[index])return;let status=rows[index].children[2];if(!status||status.querySelector('.inbox-capability-known'))return;let dot=document.createElement('span');dot.className='inbox-capability-known';dot.title='Sender currently supports JS8Mail; future Opportunistic delivery can use enhanced delivery';dot.setAttribute('aria-label','JS8Mail-capable sender');dot.setAttribute('role','img');dot.textContent='●';status.append(dot)})};
let inboxCapabilityStyle=document.createElement('style');inboxCapabilityStyle.textContent='.inbox-capability-known{display:inline-block;color:#16a34a;font-size:1.05em;line-height:1;margin-left:.2em;vertical-align:middle;cursor:help}';document.head.appendChild(inboxCapabilityStyle);
// Keep the capability marker beside the peer it describes, on the same line.
const moveInboxCapabilityHint=renderInbox;renderInbox=items=>{moveInboxCapabilityHint(items);let rows=[...document.querySelectorAll('#inbox tr[data-inbox-row]')];(items||[]).forEach((item,index)=>{if(item?.protocol!=='standard'||!item.peer_js8m||!rows[index])return;let sender=rows[index].children[1],status=rows[index].children[2],dot=status?.querySelector('.inbox-capability-known');if(sender&&dot) sender.append(' ',dot)})};
const ensureInboxCapabilityDot=renderInbox;renderInbox=items=>{ensureInboxCapabilityDot(items);let rows=[...document.querySelectorAll('#inbox tr[data-inbox-row]')];(items||[]).forEach((item,index)=>{if(!item?.peer_js8m||!rows[index])return;let sender=rows[index].children[1],dot=sender?.querySelector('.inbox-capability-known');if(!sender)return;if(!dot){dot=document.createElement('span');dot.className='inbox-capability-known';dot.setAttribute('aria-label','JS8Mail capable');dot.setAttribute('role','img');dot.textContent='●';sender.append(' ',dot)}dot.title='JS8Mail capable'})};
const inboxMessageWithSubject=showInboxMessage;showInboxMessage=async item=>{await inboxMessageWithSubject(item);let content=document.getElementById('message-modal-content');if(!content||content.querySelector('[data-message-subject]'))return;let subject=document.createElement('p');subject.dataset.messageSubject='true';subject.innerHTML=`<b>Subject:</b> ${esc(item?.subject||'(no subject)')}`;let first=content.querySelector('p');if(first)first.before(subject);else content.prepend(subject)};
// Keep the release label and update popup in sync with the package version.
document.querySelector('.version-button')?.replaceChildren(document.createTextNode('v0.0.8d · Updates'));
const showReleaseInfo=showVersionInfo;showVersionInfo=()=>{showReleaseInfo();let title=document.querySelector('#version-modal h2');if(title)title.textContent='JS8Mail v0.0.8d';let list=document.querySelector('#version-modal ul');if(list)list.innerHTML='<li>Reassembles standard and JS8Mail activity frames, including bare multiframe receipts.</li><li>Preserves partial multipart mail and correlates selective acknowledgements and final delivery receipts.</li><li>Serializes TX trains, protects RX response windows, and retains routes across busy-radio deferrals.</li><li>Improves inbox selection, stable compact previews, protocol labels, and active-band RF evidence.</li><li>Adds semantic message-route graph outcomes, compact latest-route details, collision-aware labels, and readable sizing for long callsigns.</li><li>Color-codes Live RF Activity labels to match reciprocal, active one-way, aged, and JS8Mail evidence links.</li><li>Opportunistic mode actively learns JS8M capability from marked first contact, while Standard mode remains a complete one-message opt-out.</li><li>Group broadcasts now omit the JS8Mail marker to save airtime; capability records remain valid for seven days and refresh with valid JS8M evidence.</li><li>Queues stored-message retrieval after a JS8Call YES MSG ID response, retries safely outside the receive callback, and restores pending collection after daemon restart.</li><li>Special thanks to F4LPU for the message-graph readability report, and to everyone who helped with the local and on-air testing behind this release.</li>'};
document.querySelector('.version-button')?.replaceChildren(document.createTextNode('v0.0.8d · Updates'));
const addSchedulerUpdates=showVersionInfo;showVersionInfo=()=>{addSchedulerUpdates();let list=document.querySelector('#version-modal ul');if(list)list.insertAdjacentHTML('afterbegin','<li>Emergency scheduler fix removes a permanent station-wide airtime lock that could block all queued RF until restart.</li><li>Budget waits now wake at the next eligible window instead of blindly delaying another full 15 minutes; per-message safety limits remain active.</li>')};
</script>"""

# Keep the small initial HTML paint in sync with the release metadata that the
# final script also applies after the page loads.
PAGE = PAGE.replace("0.0.7", "0.0.9").replace("0.0.8d", "0.0.9")


class Handler(BaseHTTPRequestHandler):
    service: MailService
    client: Js8CallClient
    loop: asyncio.AbstractEventLoop
    status: dict[str, Any]
    announced_destinations: set[str]
    capability_advertised_destinations: set[str]
    airtime_budget: AirtimeBudget
    message_budgets: dict[str, AirtimeBudget]
    tx_lock: asyncio.Lock
    last_tx_at_ms: int | None
    next_tx_not_before_ms: int | None
    active_transaction_id: int | None
    auto_speed: bool

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            self.service.database.close_thread_connection()

    async def _maybe_adapt_speed(self, peer: str) -> None:
        """Apply the conservative per-peer speed policy before a payload."""
        try:
            current = int(self.status.get("speed", ""))
        except (TypeError, ValueError):
            return
        if current not in SPEED_AIRTIME_MS:
            return
        raw = self.service.database.speed_evidence(
            str(self.status.get("callsign", "")),
            peer,
            str(self.status.get("band", "")),
        )
        evidence = {
            speed: SpeedEvidence(
                successes=int(values.get("successes", 0)),
                failures=int(values.get("failures", 0)),
                average_snr=(
                    float(values["average_snr"])
                    if isinstance(values.get("average_snr"), (int, float))
                    else None
                ),
            )
            for speed, values in raw.items()
        }
        decision = AdaptiveSpeedPolicy().recommend(current, evidence)
        self.status["speed_recommendation"] = {
            "peer": peer,
            "speed": decision.speed,
            "changed": decision.changed,
            "explanation": decision.explanation,
        }
        self.service.database.audit(
            "radio.speed_recommendation",
            {"peer": peer, "speed": decision.speed, "changed": decision.changed},
        )
        if self.auto_speed and decision.changed:
            try:
                await self.client.set_speed(decision.speed)
            except (ConnectionError, OSError, RuntimeError):
                self.service.database.audit(
                    "radio.speed_change_unavailable", {"peer": peer, "speed": decision.speed}
                )
            else:
                self.status["speed"] = decision.speed
                self.service.database.audit(
                    "radio.speed_changed", {"peer": peer, "speed": decision.speed}
                )

    def reply(self, code: int, value: Any, content_type: str = "application/json") -> None:
        data = value.encode() if isinstance(value, str) else json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self.reply(200, PAGE, "text/html")
        elif path == "/api/status":
            self.reply(200, self.status)
        elif path == "/api/inbox":
            self.reply(200, self.service.database.list_inbox())
        elif path == "/api/groups":
            self.reply(200, self.service.database.list_groups())
        elif path == "/api/messages":
            active_id = self.status.get("tx_message_id")
            query = parse_qs(urlparse(self.path).query)
            history = query.get("history", [""])[0].strip().lower()
            # The live UI gets a bounded recent timeline. Full durable history
            # remains in SQLite and can be requested explicitly for diagnostics.
            views = self.service.message_views(attempt_limit=None if history == "all" else 80)
            for view in views:
                view["tx_active"] = bool(active_id and view.get("id") == active_id)
            self.reply(200, views)
        elif path == "/api/control-events":
            self.reply(200, self.service.database.recent_control_events())
        elif path == "/api/observations":
            # Keep the compact panel focused on useful RF/protocol evidence.
            # PTT transitions and TX.FRAME preparation events are frequent
            # implementation noise and can otherwise hide partial messages
            # or capability negotiations from the operator.
            observations = self.service.database.recent_observations(60)
            noisy_events = {"RIG.PTT", "TX.FRAME"}
            visible = [
                item for item in observations if str(item.get("event_type", "")) not in noisy_events
            ][:12]
            self.reply(200, visible)
        elif path == "/api/graph":
            query = parse_qs(urlparse(self.path).query)
            message_id = query.get("message_id", [""])[0]
            origin = query.get("origin", [""])[0]
            band = query.get("band", [""])[0] or str(self.status.get("band", ""))
            if not message_id or not origin:
                self.reply(400, {"error": "message_id and origin are required"})
            else:
                self.reply(200, self.service.message_graph(message_id, origin, band=band))
        elif path == "/api/live-graph":
            query = parse_qs(urlparse(self.path).query)
            band = query.get("band", [""])[0] or str(self.status.get("band", ""))
            self.reply(
                200,
                self.service.live_activity_graph(
                    band, local_callsign=str(self.status.get("callsign", ""))
                ),
            )
        elif path == "/api/stations":
            band = parse_qs(urlparse(self.path).query).get("band", [""])[0] or str(
                self.status.get("band", "")
            )
            self.reply(
                200,
                self.service.station_views(
                    band=band,
                    exclude_callsign=str(self.status.get("callsign", "")),
                ),
            )
        elif path == "/api/capability":
            callsign = parse_qs(urlparse(self.path).query).get("callsign", [""])[0]
            if not callsign.strip():
                self.reply(400, {"error": "callsign is required"})
            else:
                self.reply(
                    200,
                    {
                        "callsign": callsign.strip().upper(),
                        "js8m": self.service.is_js8m_capable(callsign),
                    },
                )
        elif path == "/api/route":
            query = parse_qs(urlparse(self.path).query)
            origin = query.get("origin", [""])[0]
            destination = query.get("destination", [""])[0]
            band = query.get("band", [""])[0] or str(self.status.get("band", ""))
            if not origin or not destination:
                self.reply(400, {"error": "origin and destination are required"})
            else:
                self.reply(200, asdict(self.service.plan_route(origin, destination, band=band)))
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > 10000:
                raise ValueError("request too large")
            payload = json.loads(self.rfile.read(size)) if size else {}
            group_parts = path.strip("/").split("/")
            if len(group_parts) == 4 and group_parts[:2] == ["api", "groups"]:
                if group_parts[3] not in {"subscribe", "unsubscribe"}:
                    raise ValueError("unknown group action")
                self.service.database.set_group_subscription(
                    unquote(group_parts[2]), group_parts[3] == "subscribe"
                )
                self.reply(200, {"ok": True})
                return
            if path == "/api/control/pause":
                self.status["paused"] = True
                self.service.database.audit("radio.automation_paused", {"source": "ui"})
                if self.client.connected:
                    future = asyncio.run_coroutine_threadsafe(self.client.halt(), self.loop)
                    try:
                        future.result(timeout=5)
                    except (ConnectionError, OSError, RuntimeError, TimeoutError):
                        # The local pause is still authoritative when the
                        # installed JS8Call build does not expose TX.HALT.
                        self.service.database.audit("radio.halt_unavailable", {"source": "ui"})
                self.reply(200, {"ok": True, "paused": True})
                return
            if path == "/api/control/resume":
                self.status["paused"] = False
                self.service.database.audit("radio.automation_resumed", {"source": "ui"})
                self.reply(200, {"ok": True, "paused": False})
                return
            if path == "/api/settings/enhanced-mode":
                mode = str(payload.get("mode", "")).strip().lower()
                if mode not in ENHANCED_MODES:
                    raise ValueError("mode must be standard, opportunistic, or required")
                self.status["enhanced_mode"] = mode
                self.service.database.set_configuration("enhanced_mode", mode)
                self.service.database.audit("settings.enhanced_mode_changed", {"mode": mode})
                self.reply(200, {"ok": True, "enhanced_mode": mode})
                return
            if path == "/api/messages":
                requested_mode = str(payload.get("enhanced_mode", "")).strip().lower()
                if not requested_mode:
                    requested_mode = str(self.status.get("enhanced_mode", "opportunistic"))
                message_id = self.service.compose(
                    str(payload.get("destination", "")),
                    str(payload.get("subject", "")),
                    str(payload.get("body", "")),
                    int(payload.get("priority", 0)),
                    requested_mode,
                )
                # Let the scheduler place the first probe behind any active
                # delivery, capability negotiation, or discovery exchange.
                # This preserves a listening opportunity for the existing
                # transaction instead of allowing a newly composed message to
                # fill the next TX slot immediately.
                self.reply(201, {"id": message_id})
                return
            if len(group_parts) == 5 and group_parts[:2] == ["api", "inbox"]:
                if group_parts[4] == "read":
                    self.service.database.mark_inbox_read(
                        unquote(group_parts[2]), unquote(group_parts[3])
                    )
                    self.reply(200, {"ok": True})
                    return
                if group_parts[4] != "delete":
                    raise ValueError("unknown inbox action")
                self.service.database.delete_inbox_message(
                    unquote(group_parts[2]), unquote(group_parts[3])
                )
                self.reply(200, {"ok": True})
                return
            parts = path.strip("/").split("/")
            if len(parts) != 4 or parts[:2] != ["api", "messages"]:
                raise ValueError("not found")
            message_id, action = parts[2], parts[3]
            if action == "cancel":
                self.service.cancel(message_id)
            elif action == "delete":
                self.service.delete(message_id)
            elif action == "retry-now":
                self.service.retry_now(message_id)
                if self.status["tx_mode"] == "automatic":
                    future = asyncio.run_coroutine_threadsafe(self.prepare(message_id), self.loop)
                    future.result(timeout=30)
            elif action == "retry":
                self.service.retry(message_id)
            elif action == "send":
                future = asyncio.run_coroutine_threadsafe(self.transmit(message_id), self.loop)
                future.result(timeout=30)
            else:
                raise ValueError("unknown action")
            self.reply(200, {"ok": True})
        except (ValueError, KeyError, json.JSONDecodeError, TimeoutError, ConnectionError) as exc:
            self.reply(400, {"error": str(exc)})

    async def transmit(self, message_id: str, selected_plan: Any | None = None) -> None:
        if self.status.get("paused"):
            raise RuntimeError("RF automation is paused")
        if self.status.get("tx_mode") != "automatic":
            raise RuntimeError("automatic RF transmission is disabled")
        message = self.service.database.get_message(message_id)
        if message is None:
            raise KeyError(message_id)
        if not hasattr(self, "capability_advertised_destinations"):
            # Keep lightweight unit-test handlers and older in-process
            # controller instances compatible with the split state.
            self.capability_advertised_destinations = set()
        if message["state"] not in {MessageState.QUEUED, MessageState.WAITING_ROUTE}:
            raise ValueError("message is not ready to send")
        destination = str(message["destination"])
        origin = str(self.status.get("callsign", "")).upper()
        # A newly queued message always gets one direct delivery attempt after
        # the short reachability probe.  A graph route may be better for a
        # later attempt, but using it for the first payload would make an
        # operator-entered destination unexpectedly relay-first.
        delivery_actions = {"direct", "multipart", "relay", "store"}
        first_delivery_attempt = not any(
            attempt["action"] in delivery_actions
            and attempt["status"] in {"started", "submitted", "failed"}
            for attempt in self.service.database.list_attempts(message_id)
        )
        # The scheduler may already have selected a route based on a fresh
        # reply. Never recompute it here: doing so used to turn an indirect
        # selection back into a direct first payload attempt.
        plan = selected_plan
        continuing_enhanced = bool(
            self.service.database.list_message_parts(
                message_id, direction="outgoing", peer=destination
            )
        )
        if plan is None and continuing_enhanced:
            prior_path = self.service.database.recent_message_path(
                message_id, max_age_ms=ROUTE_HOP_PROBE_FRESH_MS
            )
            if prior_path and prior_path[0] == origin and prior_path[-1] == destination.upper():
                plan = RoutePlan(
                    RouteAction.DIRECT if len(prior_path) == 2 else RouteAction.RELAY_NOW,
                    prior_path,
                    0.0,
                    0.0,
                    0,
                    "Retaining the recent path while awaiting multipart receipts.",
                )
        if plan is None and origin and not first_delivery_attempt:
            plan = self.service.plan_route(
                origin,
                destination,
                attempted_paths=self.service.database.attempted_message_paths(message_id),
                blocked_paths=self.service.blocked_message_paths(message_id),
                band=str(self.status.get("band", "")),
            )
        if plan is not None and getattr(plan, "action", None) == "defer":
            raise RuntimeError("no usable route selected")
        path = plan.path if plan is not None else (origin, destination)
        enhanced_mode = str(
            message.get("enhanced_mode") or self.status.get("enhanced_mode", "opportunistic")
        ).lower()
        # Standard is a complete opt-out for this message: do not wait for
        # CAP and do not emit the readable marker that invites a peer to begin
        # capability discovery. Opportunistic uses the marker on first contact.
        announce = enhanced_mode != "standard" and destination not in self.announced_destinations
        standard_payload = format_standard_user_payload(
            str(message.get("subject", "")), str(message["body"])
        )
        peer = (
            self.service.database.peer_capabilities(destination)
            if enhanced_mode != "standard"
            else None
        )
        capability_attempts = [
            attempt
            for attempt in self.service.database.list_attempts(message_id)
            if attempt["action"] == "capability" and attempt["status"] == "submitted"
        ]
        capability_window_ms = capability_response_window_ms(path, self.status.get("speed", 0))
        if (
            enhanced_mode == "required"
            and not destination.startswith("@")
            and peer is None
            and capability_attempts
        ):
            capability_sent_at = int(capability_attempts[-1]["created_at_ms"])
            completed = [
                attempt
                for attempt in self.service.database.list_attempts(message_id)
                if attempt["action"] == "capability_tx"
                and attempt["status"] == "complete"
                and int(attempt["created_at_ms"]) >= capability_sent_at
            ]
            elapsed = utc_now_ms() - int(completed[-1]["created_at_ms"]) if completed else 0
            if (
                (not completed and utc_now_ms() - capability_sent_at < CAPABILITY_TX_START_GRACE_MS)
                or (completed and elapsed < capability_window_ms)
                or self.status.get("tx_train_pending")
            ):
                remaining = (
                    max(30_000, CAPABILITY_TX_START_GRACE_MS - (utc_now_ms() - capability_sent_at))
                    if not completed
                    else max(5_000, capability_window_ms - elapsed)
                )
                self.service.database.record_attempt(
                    message_id,
                    "capability_wait",
                    destination,
                    "waiting",
                    "waiting for CAP RF completion or JS8Mail capability response",
                )
                self.service.database.transition_message(message_id, MessageState.WAITING_ROUTE)
                self.service.database.defer_message(
                    message_id,
                    remaining,
                    "waiting for capability response before ordinary fallback",
                )
                return
            self.service.database.record_attempt(
                message_id,
                "capability_timeout",
                destination,
                "fallback",
                "no capability response; using ordinary JS8Call delivery",
            )
        enhanced_parts = (
            split_human_message(message_id, str(message["body"]))
            if peer is not None and "MP" in peer[1]
            else ()
        )
        selected_enhanced_parts = enhanced_parts
        if enhanced_parts:
            bitmap = self.service.database.outgoing_part_bitmap(message_id, len(enhanced_parts))
            missing = [part for part in enhanced_parts if not bitmap & (1 << (part.number - 1))]
            # After a full PA, a missing DELIVERED receipt may be nudged by
            # repeating only the final part on the later retry opportunity.
            selected_enhanced_parts = (missing[0] if missing else enhanced_parts[-1],)
        wire_texts: tuple[str, ...]
        if destination.startswith("@"):
            # Group traffic is ordinary JS8Call broadcast mail. Do not add a
            # JS8Mail marker: it is not needed for group delivery or capability
            # discovery, and omitting it preserves airtime and avoids confusing
            # group listeners with a directed capability invitation.
            wire_texts = (format_ordinary_message(destination, standard_payload),)
            action = "group_broadcast"
            target = destination
            detail = "broadcast submitted; no ACK expected"
        elif plan is not None and len(path) >= 3:
            payloads = tuple(
                format_human_data_part(part, subject=str(message.get("subject", "")))
                for part in selected_enhanced_parts
            ) or (str(message["body"]),)
            wire_texts = tuple(format_relay_message(path, payload) for payload in payloads)
            action = "relay"
            target = path[1]
            detail = f"discovered path: {'→'.join(path)}"
            if enhanced_parts:
                detail += f"; part {selected_enhanced_parts[0].number}/{len(enhanced_parts)}"
        elif enhanced_parts:
            wire_texts = tuple(
                format_ordinary_message(
                    destination,
                    format_human_data_part(part, subject=str(message.get("subject", ""))),
                )
                for part in selected_enhanced_parts
            )
            action = "multipart"
            target = destination
            detail = f"JS8Mail part {selected_enhanced_parts[0].number}/{len(enhanced_parts)}; waiting for PA before next part"
        else:
            wire_texts = (format_ordinary_message(destination, standard_payload, announce),)
            action = "direct"
            target = destination
            detail = "initial direct attempt"
        if origin and len(path) >= 2:
            self.service.database.record_message_path(message_id, path)
        # Advertise before the first payload so a JS8Mail peer can recognize
        # and prepare for enhanced framing. This is opportunistic: no response
        # is awaited, and ordinary stations remain valid recipients.
        if (
            enhanced_mode == "required"
            and not destination.startswith("@")
            and destination not in self.capability_advertised_destinations
            and self.service.database.peer_capabilities(destination) is None
        ):
            try:
                capability_text = (
                    f"{destination} {format_capability()}"
                    if len(path) < 3
                    else format_relay_text(path, format_capability())
                )
                self.status["pending_capability_message_id"] = message_id
                self.status["pending_capability_peer"] = destination.upper()
                await Handler.send_rf(self, capability_text, message_id)
                self.status.setdefault("capability_last_sent", {})[destination.upper()] = (
                    utc_now_ms()
                )
                self.service.database.record_attempt(
                    message_id,
                    "capability",
                    destination,
                    "submitted",
                    f"JS8Mail capability advertisement; response window "
                    f"{capability_window_ms // 1000}s starts after RF completion",
                )
                self.capability_advertised_destinations.add(destination)
                self.announced_destinations.add(destination)
                self.service.database.record_attempt(
                    message_id,
                    "capability_wait",
                    destination,
                    "waiting",
                    f"waiting for JS8Mail capability response after RF completion; window "
                    f"{capability_window_ms // 1000}s for {max(1, len(path) - 1)} hop(s)",
                )
                self.service.database.transition_message(message_id, MessageState.WAITING_ROUTE)
                self.service.database.defer_message(
                    message_id,
                    capability_outbound_ms(path, capability_text, self.status.get("speed", 0))
                    + capability_window_ms
                    + TX_TRAIN_QUIET_MS,
                    f"waiting for capability response before ordinary fallback "
                    f"({capability_window_ms // 1000}s estimated)",
                )
                return
            except AirtimeBudgetExceeded as exc:
                self.status["pending_capability_message_id"] = None
                self.status["pending_capability_peer"] = None
                detail = (
                    f"{exc.scope} airtime budget exhausted; radio is idle and policy blocked TX"
                )
                self.service.database.record_attempt(
                    message_id,
                    "capability",
                    destination,
                    "failed" if exc.scope == "per-message-total" else "deferred",
                    detail,
                )
                if exc.scope == "per-message-total":
                    self.service.database.transition_message(message_id, MessageState.FAILED)
                else:
                    self.service.database.defer_message(
                        message_id, 15 * 60 * 1000, detail, increment_retry=False
                    )
                return
            except (ConnectionError, OSError, RuntimeError) as exc:
                self.status["pending_capability_message_id"] = None
                self.status["pending_capability_peer"] = None
                self.service.database.record_attempt(
                    message_id,
                    "capability",
                    destination,
                    "deferred",
                    f"JS8Call unavailable or busy: {radio_exception_reason(exc)}",
                )
                self.service.database.defer_message(
                    message_id,
                    30_000,
                    "CAP handoff deferred until JS8Call is available; retrying in 30 seconds",
                    increment_retry=False,
                )
                return
        self.service.database.record_attempt(message_id, action, target, "started", detail)
        self.service.database.transition_message(message_id, MessageState.WAITING_ROUTE)
        self.service.database.transition_message(message_id, MessageState.IN_PROGRESS)
        try:
            speed = int(self.status.get("speed", 0))
        except (TypeError, ValueError):
            speed = 0
        transaction_id = self.service.database.begin_transmission_transaction(
            message_id,
            action,
            target,
            destination,
            tuple(path),
            hashlib.sha256("\n".join(wire_texts).encode("utf-8")).hexdigest(),
            sum(
                estimate_airtime_ms(text, speed if speed in SPEED_AIRTIME_MS else 0)
                for text in wire_texts
            ),
            delivery_response_window_ms(action, tuple(path), speed),
            int(message.get("retry_count", 0)),
            str(self.status.get("band", "")),
        )
        self.active_transaction_id = transaction_id
        try:
            if target and not target.startswith("@"):
                await self._maybe_adapt_speed(target)
            for part in enhanced_parts:
                self.service.database.upsert_message_part(
                    part.message_id,
                    part.number,
                    part.total,
                    part.payload,
                    direction="outgoing",
                    peer=destination,
                )
            for text in wire_texts:
                current = self.service.database.get_message(message_id)
                if current is None or current["state"] in {
                    MessageState.CANCELLED,
                    MessageState.FAILED,
                    MessageState.EXPIRED,
                }:
                    self.service.database.mark_transmission_unconfirmed(transaction_id)
                    if self.active_transaction_id == transaction_id:
                        self.active_transaction_id = None
                    self.service.database.record_attempt(
                        message_id,
                        action,
                        target,
                        "cancelled",
                        "remaining frames suppressed after operator cancellation",
                    )
                    return
                await Handler.send_rf(self, text, message_id)
        except (ConnectionError, OSError, RuntimeError) as exc:
            self.service.database.mark_transmission_unconfirmed(transaction_id)
            if self.active_transaction_id == transaction_id:
                self.active_transaction_id = None
            self.service.database.record_attempt(
                message_id, action, target, "failed", radio_exception_reason(exc)
            )
            raise
        except Exception as exc:
            self.service.database.mark_transmission_unconfirmed(transaction_id)
            if self.active_transaction_id == transaction_id:
                self.active_transaction_id = None
            self.service.database.record_attempt(
                message_id,
                action,
                target,
                "failed",
                f"local error {radio_exception_reason(exc)[:240]}",
            )
            self.service.database.audit(
                "message.transmit_unexpected_error",
                {
                    "message_id": message_id,
                    "action": action,
                    "error": type(exc).__name__,
                    "detail": str(exc)[:240],
                    "traceback": traceback.format_exc(limit=8)[-2000:],
                },
            )
            raise
        current = self.service.database.get_message(message_id)
        if current is not None and current["state"] == MessageState.ACKNOWLEDGED:
            self.service.database.mark_transmission_unconfirmed(transaction_id)
            if self.active_transaction_id == transaction_id:
                self.active_transaction_id = None
            self.service.database.record_attempt(
                message_id,
                action,
                target,
                "held",
                "remaining frames suppressed after automatic ACK hold",
            )
            return
        self.service.database.mark_transmission_submitted(transaction_id)
        self.service.database.record_attempt(
            message_id, action, target, "submitted", "queued in JS8Call for next TX cycle"
        )
        if action == "group_broadcast":
            self.service.database.record_attempt(
                message_id,
                action,
                target,
                "waiting",
                "broadcast queued in JS8Call; awaiting RF completion, no recipient ACK expected",
            )
        self.service.database.audit(
            "message.submitted_to_js8call",
            {
                "message_id": message_id,
                "text_length": sum(len(text) for text in wire_texts),
                "frames": len(wire_texts),
            },
        )
        self.announced_destinations.add(destination)

    async def transmit_store(self, message_id: str, custodian: str) -> None:
        """Offer a legacy-compatible message to one remote custodian."""
        message = self.service.database.get_message(message_id)
        if message is None:
            raise KeyError(message_id)
        if message["state"] not in {MessageState.QUEUED, MessageState.WAITING_ROUTE}:
            raise ValueError("message is not ready for storage")
        destination = str(message["destination"])
        enhanced_mode = str(
            message.get("enhanced_mode") or self.status.get("enhanced_mode", "opportunistic")
        ).lower()
        # CAP is sent before the first enhanced/ordinary payload. Do not let
        # the custodian fallback bypass that negotiation on the next scheduler
        # pass while the CAP frame is still in flight or awaiting a response.
        if (
            enhanced_mode == "required"
            and not destination.startswith("@")
            and self.service.database.peer_capabilities(destination) is None
        ):
            capability_attempts = [
                attempt
                for attempt in self.service.database.list_attempts(message_id)
                if attempt["action"] == "capability" and attempt["status"] == "submitted"
            ]
            if capability_attempts:
                sent_at = int(capability_attempts[-1]["created_at_ms"])
                completed = [
                    attempt
                    for attempt in self.service.database.list_attempts(message_id)
                    if attempt["action"] == "capability_tx"
                    and attempt["status"] == "complete"
                    and int(attempt["created_at_ms"]) >= sent_at
                ]
                elapsed = utc_now_ms() - (
                    int(completed[-1]["created_at_ms"]) if completed else sent_at
                )
                cap_path = self.service.database.recent_message_path(
                    message_id, max_age_ms=60 * 60 * 1000
                )
                if not cap_path or cap_path[-1].upper() != destination.upper():
                    cap_path = (str(self.status.get("callsign", "")), destination)
                window = (
                    capability_response_window_ms(cap_path, self.status.get("speed", 0))
                    if completed
                    else CAPABILITY_TX_START_GRACE_MS
                )
                if elapsed < window or self.status.get("tx_train_pending"):
                    remaining = max(5_000, window - elapsed)
                    self.service.database.record_attempt(
                        message_id,
                        "capability_wait",
                        destination,
                        "waiting",
                        "waiting for JS8Mail capability response before store fallback",
                    )
                    self.service.database.transition_message(message_id, MessageState.WAITING_ROUTE)
                    self.service.database.defer_message(
                        message_id,
                        remaining,
                        "waiting for capability response before store fallback",
                    )
                    return
        origin = str(self.status.get("callsign", "")).upper()
        # Opportunistic/required mode describes what the sender would like to
        # use; it is not proof that a compatibility custodian can parse the
        # enhanced envelope.  JS8Call's MSG TO: store operation may carry an
        # opaque body through an ordinary station, so keep that path readable
        # unless this particular custodian has unexpired JS8M evidence.  This
        # also prevents a stale capability row from causing repeated J8M1
        # offers after a restart.
        custodian_capability = self.service.database.peer_capabilities(custodian)
        enhanced_store = enhanced_mode != "standard" and custodian_capability is not None
        custodian_features = (
            {feature.upper() for feature in custodian_capability[1]}
            if custodian_capability is not None
            else set()
        )
        # JS8Call's store record does not reliably preserve the original
        # author in the retrieved body. Keep origin/destination metadata for
        # custody traffic, even though direct and ordinary relay payloads are
        # intentionally compact.
        store_parts = (
            split_human_message(message_id, str(message["body"]))
            if enhanced_store and "MP" in custodian_features
            else ()
        )
        standard_payload = format_standard_user_payload(
            str(message.get("subject", "")), str(message["body"])
        )
        store_texts = tuple(
            format_store_message(
                custodian,
                destination,
                format_human_data_part(part, origin, destination, str(message.get("subject", ""))),
            )
            for part in store_parts
        ) or (format_store_message(custodian, destination, standard_payload),)
        text = "\n".join(store_texts)
        if origin:
            self.service.database.record_message_path(message_id, (origin, custodian.upper()))
        self.service.database.record_attempt(
            message_id, "store", custodian, "started", f"offer for later retrieval by {destination}"
        )
        self.service.database.upsert_custody(
            message_id, custodian, "offered", "store offer submitted"
        )
        self.service.database.transition_message(message_id, MessageState.WAITING_ROUTE)
        self.service.database.transition_message(message_id, MessageState.IN_PROGRESS)
        try:
            speed = int(self.status.get("speed", 0))
        except (TypeError, ValueError):
            speed = 0
        transaction_id = self.service.database.begin_transmission_transaction(
            message_id,
            "store",
            custodian,
            custodian,
            (origin, custodian.upper()) if origin else (custodian.upper(),),
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
            sum(
                estimate_airtime_ms(item, speed if speed in SPEED_AIRTIME_MS else 0)
                for item in store_texts
            ),
            delivery_response_window_ms(
                "store", (origin, custodian.upper()) if origin else (custodian.upper(),), speed
            ),
            int(message.get("retry_count", 0)),
            str(self.status.get("band", "")),
        )
        self.active_transaction_id = transaction_id
        try:
            await self._maybe_adapt_speed(custodian)
            for store_text in store_texts:
                current = self.service.database.get_message(message_id)
                if current is None or current["state"] in {
                    MessageState.CANCELLED,
                    MessageState.FAILED,
                    MessageState.EXPIRED,
                }:
                    self.service.database.mark_transmission_unconfirmed(transaction_id)
                    if self.active_transaction_id == transaction_id:
                        self.active_transaction_id = None
                    self.service.database.record_attempt(
                        message_id,
                        "store",
                        custodian,
                        "cancelled",
                        "remaining store frames suppressed after operator cancellation",
                    )
                    return
                await Handler.send_rf(self, store_text, message_id)
        except (ConnectionError, OSError, RuntimeError) as exc:
            self.service.database.mark_transmission_unconfirmed(transaction_id)
            if self.active_transaction_id == transaction_id:
                self.active_transaction_id = None
            self.service.database.record_attempt(
                message_id, "store", custodian, "failed", radio_exception_reason(exc)
            )
            self.service.database.upsert_custody(
                message_id, custodian, "failed", radio_exception_reason(exc)
            )
            raise
        except Exception as exc:
            self.service.database.mark_transmission_unconfirmed(transaction_id)
            if self.active_transaction_id == transaction_id:
                self.active_transaction_id = None
            self.service.database.record_attempt(
                message_id, "store", custodian, "failed", radio_exception_reason(exc)
            )
            self.service.database.upsert_custody(
                message_id, custodian, "failed", radio_exception_reason(exc)
            )
            raise
        self.service.database.mark_transmission_submitted(transaction_id)
        self.service.database.record_attempt(
            message_id,
            "store",
            custodian,
            "submitted",
            (
                f"{len(store_texts)} JS8Mail store part(s) queued in JS8Call for next TX cycle"
                if store_parts
                else (
                    "JS8Mail store envelope queued in JS8Call for next TX cycle"
                    if enhanced_store
                    else "standard-readable store offer queued in JS8Call for next TX cycle"
                )
            ),
        )

    async def prepare(self, message_id: str) -> None:
        """Probe before committing payload airtime, then use normal discovery."""
        message = self.service.database.get_message(message_id)
        if message is None:
            raise KeyError(message_id)
        destination = str(message["destination"])
        if destination.startswith("@"):
            # Group traffic is explicitly operator-addressed and must not be
            # preceded by a group-wide SNR? probe or capability fan-out.
            await self.transmit(message_id)
            return
        local_call = str(self.status.get("callsign", "")).upper()
        band = str(self.status.get("band", ""))
        fresh_answered = self.service.recent_answered_age_ms(
            destination, local_call, band=band, window_ms=ROUTE_HOP_PROBE_FRESH_MS
        )
        fresh_heard = self.service.recent_heard_age_ms(
            destination, band=band, window_ms=ROUTE_HOP_PROBE_FRESH_MS
        )
        if fresh_answered is not None or fresh_heard is not None:
            age_ms = fresh_answered if fresh_answered is not None else fresh_heard
            assert age_ms is not None
            self.service.database.record_attempt(
                message_id,
                "route_evidence",
                destination,
                "received",
                f"fresh direct RF evidence ({age_ms // 1000}s ago); skipping SNR probe",
            )
            try:
                await self.transmit(message_id)
            except (ConnectionError, OSError, RuntimeError, AirtimeBudgetExceeded) as exc:
                detail = radio_exception_reason(exc)
                self.service.database.record_attempt(
                    message_id,
                    "direct",
                    destination,
                    "deferred",
                    f"direct handoff deferred: {detail}",
                )
                self.service.database.defer_message(
                    message_id,
                    60_000,
                    f"fresh RF evidence retained; waiting to transmit: {detail}",
                    increment_retry=False,
                )
            return
        # Even when stale direct or indirect evidence exists, the first action
        # for a newly queued destination is the small direct SNR probe.  This
        # prevents spending a long JS8Call frame on a station that is not
        # currently reachable.  A response causes discovery_loop to submit
        # the first payload directly; a timeout falls through to route and
        # custodian discovery.
        probe = snr_query(destination)
        self.service.database.record_attempt(
            message_id, "snr_probe", destination, "started", "destination not recently heard"
        )
        probe_busy = False
        try:
            await Handler.send_rf(self, probe, message_id)
        except (ConnectionError, OSError, RuntimeError) as exc:
            probe_busy = True
            self.service.database.record_attempt(
                message_id,
                "snr_probe",
                destination,
                "deferred",
                f"JS8Call unavailable or busy: {radio_exception_reason(exc)}",
            )
        else:
            self.service.database.record_attempt(
                message_id, "snr_probe", destination, "submitted", "waiting for RF evidence"
            )
        self.service.database.transition_message(message_id, MessageState.WAITING_ROUTE)
        self.service.database.defer_message(
            message_id,
            30_000 if probe_busy else 60_000,
            "JS8Call busy or unavailable; retrying probe in 30 seconds"
            if probe_busy
            else "listening for probe response; discovery fallback in 1 minute(s)",
            increment_retry=not probe_busy,
        )

    async def ensure_route_first_hop_reachable(
        self, message_id: str, path: tuple[str, ...]
    ) -> bool:
        """Hold a stale route behind a cheap probe to its first RF hop.

        A temporal route can remain useful as a candidate after its evidence
        has aged, but submitting the complete message immediately can consume
        several minutes of airtime before we learn that the first relay has
        disappeared.  A direct SNR answer from the first hop is the small,
        current reachability test that permits the payload to proceed.
        """
        if len(path) < 2:
            return False
        first_hop = path[1].upper()
        local_call = str(self.status.get("callsign", "")).upper()
        band = str(self.status.get("band", ""))
        answered_age = self.service.recent_answered_age_ms(
            first_hop, local_call, band=band, window_ms=ROUTE_HOP_PROBE_FRESH_MS
        )
        if answered_age is not None:
            return True

        attempts = self.service.database.list_attempts(message_id)
        recent_probe = next(
            (
                attempt
                for attempt in reversed(attempts)
                if attempt["action"] == "route_probe"
                and str(attempt["target"]).upper() == first_hop
                and attempt["status"] in {"submitted", "deferred"}
            ),
            None,
        )
        now = utc_now_ms()
        if recent_probe is not None:
            probe_age = max(0, now - int(recent_probe["created_at_ms"]))
            if probe_age < ROUTE_HOP_PROBE_COOLDOWN_MS:
                self.service.database.transition_message(message_id, MessageState.WAITING_ROUTE)
                self.service.database.defer_message(
                    message_id,
                    min(60_000, ROUTE_HOP_PROBE_COOLDOWN_MS - probe_age),
                    f"waiting for {first_hop} SNR response; full route payload held",
                    increment_retry=False,
                )
                return False

        probe = snr_query(first_hop)
        self.service.database.record_attempt(
            message_id,
            "route_probe",
            first_hop,
            "started",
            f"stale first hop before payload; path {'→'.join(path)}",
        )
        try:
            await self.send_rf(probe, message_id)
        except (ConnectionError, OSError, RuntimeError, AirtimeBudgetExceeded) as exc:
            self.service.database.record_attempt(
                message_id, "route_probe", first_hop, "deferred", radio_exception_reason(exc)
            )
            self.service.database.transition_message(message_id, MessageState.WAITING_ROUTE)
            self.service.database.defer_message(
                message_id,
                30_000,
                "first-hop SNR probe deferred; full route payload held",
                increment_retry=False,
            )
            return False
        self.service.database.record_attempt(
            message_id,
            "route_probe",
            first_hop,
            "submitted",
            "waiting for direct reachability response before full payload",
        )
        self.service.database.transition_message(message_id, MessageState.WAITING_ROUTE)
        self.service.database.defer_message(
            message_id,
            query_response_window_ms("candidate_query_call", self.status.get("speed", 0)),
            f"waiting for {first_hop} SNR response before full route payload",
            increment_retry=False,
        )
        return False

    def log_message(self, format: str, *args: object) -> None:
        return

    async def send_rf(self, text: str, message_id: str | None = None) -> None:
        """Reserve conservative airtime before handing a frame to JS8Call."""
        async with self.tx_lock:
            await Handler._send_rf_serialized(self, text, message_id)

    async def _send_rf_serialized(self, text: str, message_id: str | None = None) -> None:
        """Submit one frame only after the prior TX and RX hold have cleared."""
        if self.status.get("paused"):
            raise RuntimeError("RF automation is paused")
        if self.status.get("tx_mode") != "automatic":
            raise RuntimeError("automatic RF transmission is disabled")
        # A PTT-off event between JS8Call frames is not an available slot.
        # The event reader settles the complete RF train independently of
        # this scheduler coroutine, so waiting here cannot deadlock it.
        train_deadline = asyncio.get_running_loop().time() + 10 * 60
        while self.status.get("tx_train_pending"):
            if asyncio.get_running_loop().time() >= train_deadline:
                raise RuntimeError("previous JS8Call RF train did not settle")
            await asyncio.sleep(0.25)
        incoming_until = int(self.status.get("incoming_directed_until_ms", 0) or 0)
        if incoming_until > utc_now_ms():
            raise RuntimeError("JS8Call is receiving a directed message")
        activity_until = int(self.status.get("incoming_activity_until_ms", 0) or 0)
        if activity_until > utc_now_ms():
            raise RuntimeError("JS8Call recently decoded RF activity")
        # If JS8Call exposes the live PTT state, never queue behind an active
        # transmission. The timeout is deliberately bounded so a broken or
        # stale status event cannot deadlock the daemon forever.
        if self.status.get("radio_activity") == "TX":
            deadline = asyncio.get_running_loop().time() + 180
            while (
                self.status.get("radio_activity") == "TX"
                and asyncio.get_running_loop().time() < deadline
            ):
                await asyncio.sleep(0.25)
            if self.status.get("radio_activity") == "TX":
                raise RuntimeError("JS8Call is still transmitting")
        now = utc_now_ms()
        not_before = self.next_tx_not_before_ms or 0
        if self.last_tx_at_ms is not None:
            not_before = max(not_before, self.last_tx_at_ms + AUTOMATED_TX_GAP_MS)
        if not_before > now:
            wait_ms = not_before - now
            if wait_ms > 0:
                self.service.database.audit(
                    "radio.tx_pacing_wait",
                    {"message_id": message_id, "wait_ms": wait_ms},
                )
                await asyncio.sleep(wait_ms / 1000)
                now = utc_now_ms()
        if self.status.get("paused") or self.status.get("tx_mode") != "automatic":
            raise RuntimeError("RF automation is paused or disabled")
        if (
            self.status.get("tx_train_pending")
            or int(self.status.get("incoming_directed_until_ms", 0) or 0) > now
            or int(self.status.get("incoming_activity_until_ms", 0) or 0) > now
        ):
            raise RuntimeError("JS8Call is receiving or transmitting; TX deferred")
        try:
            speed = int(self.status.get("speed", 0))
        except (TypeError, ValueError):
            speed = 1
        if speed not in SPEED_AIRTIME_MS:
            speed = 0
        airtime_ms = estimate_airtime_ms(text, speed)
        now = utc_now_ms()
        if not self.airtime_budget.can_spend_at(airtime_ms, now):
            if message_id is not None:
                self.service.database.record_attempt(
                    message_id,
                    "airtime_budget",
                    "radio",
                    "blocked",
                    f"rolling airtime budget exhausted at speed {speed}",
                )
            self.service.database.audit(
                "radio.airtime_blocked",
                {"message_id": message_id, "estimate_ms": airtime_ms, "speed": speed},
            )
            raise AirtimeBudgetExceeded(
                "rolling", self.airtime_budget.next_available_at(airtime_ms, now)
            )
        message_budget = None
        if message_id is not None:
            message_budget = self.message_budgets.setdefault(
                message_id,
                AirtimeBudget(
                    window_limit_ms=MESSAGE_BURST_LIMIT_MS,
                    message_limit_ms=MESSAGE_TOTAL_LIMIT_MS,
                ),
            )
            saved_message_airtime = self.service.database.message_airtime_used(message_id)
            if saved_message_airtime and message_budget.message_used_ms == 0:
                message_budget.message_used_ms = saved_message_airtime
            if not message_budget.can_spend_at(airtime_ms, now):
                self.service.database.record_attempt(
                    message_id,
                    "airtime_budget",
                    "message",
                    "blocked",
                    f"per-message airtime budget exhausted at speed {speed}",
                )
                message_limit = message_budget.message_limit_ms
                scope = (
                    "per-message-total"
                    if message_limit is not None
                    and message_budget.message_used_ms + airtime_ms > message_limit
                    else "per-message-window"
                )
                raise AirtimeBudgetExceeded(
                    scope, message_budget.next_available_at(airtime_ms, now)
                )
        # A short, independent protocol LED makes API/RF handoff visible even
        # when the radio remains in its normal RX state.
        # Reserve before handing text to JS8Call. The reservation is
        # deliberately conservative if the daemon dies after submission; a
        # future ledger can reconcile it against observed RF. A definite
        # preflight failure must not be allowed to consume this reservation.
        if not self.airtime_budget.spend_at(airtime_ms, now):
            raise AirtimeBudgetExceeded("rolling")
        if message_budget is not None and not message_budget.spend_at(airtime_ms, now):
            raise AirtimeBudgetExceeded("per-message")
        if message_budget is not None and message_id is not None:
            self.service.database.save_message_airtime(message_id, message_budget.message_used_ms)
        self.service.database.save_airtime_state(
            self.airtime_budget.window_started_at_ms,
            self.airtime_budget.window_used_ms,
            self.airtime_budget.message_used_ms,
        )
        self.status["js8_activity_until_ms"] = utc_now_ms() + 1_000
        if message_id is not None:
            self.status["tx_message_id"] = message_id
        self.status["tx_reserved_ms"] = airtime_ms
        try:
            if (
                self.status.get("paused")
                or self.status.get("tx_train_pending")
                or (int(self.status.get("incoming_directed_until_ms", 0) or 0) > utc_now_ms())
                or (int(self.status.get("incoming_activity_until_ms", 0) or 0) > utc_now_ms())
            ):
                raise RuntimeError("radio slot changed before API handoff")
            await self.client.send_message(text)
        except Exception:
            self.status["tx_reserved_ms"] = 0
            self.status["tx_message_id"] = None
            raise
        self.last_tx_at_ms = utc_now_ms()
        # The API submission is not the end of RF transmission. Hold the next
        # automated submission past the conservative airtime estimate and a
        # receive window for ACKs/replies. A later RIG.PTT TX->RX event can
        # extend this hold from the actual end of transmission.
        self.next_tx_not_before_ms = self.last_tx_at_ms + airtime_ms + AUTOMATED_RX_WINDOW_MS
        self.service.database.audit(
            "radio.airtime_reserved",
            {"message_id": message_id, "estimate_ms": airtime_ms, "speed": speed},
        )


async def run(args: argparse.Namespace) -> None:
    database = Database(Path(args.database).expanduser().resolve())
    service = MailService(database)
    for group, description in DEFAULT_GROUPS:
        database.ensure_group(group, description, subscribed=group == "@JS8MAIL")
    database.repair_group_observations()
    database.reconcile_forwarded_inbox_messages()
    configured_mode = database.get_configuration("enhanced_mode", "opportunistic")
    if configured_mode not in ENHANCED_MODES:
        configured_mode = "opportunistic"
    client = Js8CallClient(args.host, args.port)
    loop = asyncio.get_running_loop()
    saved_airtime = database.airtime_state()
    saved_window_start = saved_airtime.get("window_started_at_ms")
    airtime_budget = AirtimeBudget(
        # The radio-wide budget is governed by its rolling duty-cycle
        # window.  Lifetime ceilings belong to individual messages below;
        # applying one here would eventually block the entire station after
        # a few unrelated tests and would survive only until restart.
        message_limit_ms=None,
        window_used_ms=int(saved_airtime.get("window_used_ms") or 0),
        window_started_at_ms=int(saved_window_start) if saved_window_start is not None else None,
    )
    status: dict[str, Any] = {
        "connected": False,
        "host": args.host,
        "port": args.port,
        "tx_mode": args.tx_mode,
        "paused": False,
        "callsign": "",
        "band": "",
        "dial_frequency": None,
        "speed": "unknown",
        "enhanced_mode": configured_mode,
        "radio_activity": "RX",
        "next_tx_not_before_ms": 0,
        "dcd_until_ms": 0,
        "js8_activity_until_ms": 0,
        "tx_message_id": None,
        "tx_reserved_ms": 0,
        "incoming_directed_until_ms": 0,
        "incoming_directed_completion_guard_until_ms": 0,
        "incoming_activity_until_ms": 0,
        "last_rx_activity_ms": 0,
        "rx_activity_guard_slots": 0,
        "active_transaction_id": None,
        "tx_train_pending": False,
        "pending_capability_message_id": None,
        "pending_capability_peer": None,
        "speed_recommendation": None,
    }
    handler: type[Handler] = type(
        "BoundHandler",
        (Handler,),
        {
            "service": service,
            "client": client,
            "loop": loop,
            "status": status,
            "announced_destinations": set(),
            "capability_advertised_destinations": set(),
            "airtime_budget": airtime_budget,
            "message_budgets": {},
            "tx_lock": asyncio.Lock(),
            "last_tx_at_ms": None,
            "next_tx_not_before_ms": None,
            "active_transaction_id": None,
            "auto_speed": args.auto_speed,
        },
    )
    # The HTTP server creates request-handler instances, but the background
    # scheduler also needs a bound Handler object. Calling methods through the
    # dynamic class itself loses ``self`` when one handler method calls another
    # (notably _maybe_adapt_speed), producing misleading TypeErrors.
    controller = object.__new__(handler)
    controller.service = service
    controller.client = client
    controller.loop = loop
    controller.status = status
    controller.announced_destinations = set()
    controller.capability_advertised_destinations = set()
    controller.airtime_budget = airtime_budget
    controller.message_budgets = {}
    controller.tx_lock = asyncio.Lock()
    controller.last_tx_at_ms = None
    controller.next_tx_not_before_ms = None
    controller.active_transaction_id = None
    controller.auto_speed = args.auto_speed
    server = ThreadingHTTPServer((args.ui_host, args.ui_port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"JS8Mail UI: http://{args.ui_host}:{args.ui_port}", flush=True)
    if args.open_browser:
        # Used by the packaged desktop executable; the development launcher
        # stays headless unless explicitly asked to open a browser.
        webbrowser.open(f"http://{args.ui_host}:{args.ui_port}")
    delay = 1.0
    query_scheduler = QueryScheduler()
    inbox_scheduler = QueryScheduler(base_delay_ms=1_800_000, max_delay_ms=21_600_000)
    last_inbox_query = database.latest_audit_time(
        "discovery.query_submitted", "action", "messages_query"
    )
    if last_inbox_query is not None:
        elapsed = max(0, utc_now_ms() - last_inbox_query)
        inbox_scheduler.restore(
            "inbox:broadcast",
            int(asyncio.get_running_loop().time() * 1000),
            max(0, 1_800_000 - elapsed),
        )
    # MID is only locally unique; the sender is part of the reassembly key.
    reassembly: dict[tuple[str, str], MultipartAccumulator] = {}
    pending_call_queries: list[PendingCallQuery] = []
    # (custodian, JS8Call message id) -> (next retry time, submitted count).
    # This is intentionally reconstructed from audit events on startup: a
    # QUERY MSG request must survive a daemon restart even though the remote
    # JS8Call store is separate from our mailbox database.
    pending_retrievals: dict[tuple[str, int], tuple[int, int]] = {}
    completed_retrievals: set[tuple[str, int]] = set()
    max_retrieval_attempts = 4
    retrieval_retry_delay_ms = 45_000

    def retrieval_key_from_payload(payload: dict[str, Any]) -> tuple[str, int] | None:
        custodian = str(payload.get("custodian", "")).strip().upper()
        try:
            stored_id = int(payload.get("js8call_message_id"))
        except (TypeError, ValueError):
            return None
        if not custodian or not 0 <= stored_id <= 2_147_483_647:
            return None
        return custodian, stored_id

    def restore_pending_retrievals(now_ms: int) -> None:
        """Recover retrievals that were announced or submitted before restart."""
        latest: dict[tuple[str, int], tuple[int, str, int]] = {}
        since_ms = now_ms - 7 * 24 * 60 * 60 * 1000
        events: list[tuple[int, str, dict[str, Any]]] = []
        for event_type in (
            "inbox.retrieval_pending",
            "inbox.retrieval_submitted",
            "inbox.retrieval_failed",
            "inbox.retrieval_completed",
            "inbox.retrieval_exhausted",
        ):
            for event in database.recent_audit_events(event_type, since_ms):
                events.append((int(event["created_at_ms"]), event_type, event["payload"]))
        for created_at_ms, event_type, payload in sorted(events):
            key = retrieval_key_from_payload(payload)
            if key is None:
                continue
            try:
                attempts = int(payload.get("attempt", 0))
            except (TypeError, ValueError):
                attempts = 0
            latest[key] = (created_at_ms, event_type, max(0, attempts))
        for key, (created_at_ms, event_type, attempts) in latest.items():
            if event_type == "inbox.retrieval_completed":
                completed_retrievals.add(key)
                continue
            if event_type == "inbox.retrieval_exhausted":
                continue
            # A submitted request may have been in flight when the daemon
            # stopped. Re-send it promptly after the new JS8Call connection is
            # ready; a duplicate QUERY MSG is safe and the remote store will
            # remove the item after one successful retrieval.
            due_at = min(now_ms, created_at_ms + retrieval_retry_delay_ms)
            pending_retrievals[key] = (due_at, min(attempts, max_retrieval_attempts - 1))

    restore_pending_retrievals(utc_now_ms())
    capability_last_sent: dict[str, int] = {}
    pending_capability_advertisements: dict[str, int] = {}
    pending_capability_reasons: dict[str, str] = {}
    status["capability_last_sent"] = capability_last_sent
    tx_train = TxTrain()
    tx_settle_task: asyncio.Task[None] | None = None

    async def settle_rf_train() -> None:
        """Complete an operation only after its last PTT-off remains quiet."""
        await asyncio.sleep(TX_TRAIN_QUIET_MS / 1000)
        complete_rf_train(database, controller, status, tx_train)

    # Enhanced PA/DELIVERED responses are queued after RX rather than handed
    # to JS8Call from inside the RX event task. JS8Call deliberately rejects
    # automatic TX while it is still completing a directed receive.
    pending_protocol_replies: dict[str, tuple[int, str, str]] = {}
    retrieval_capability_last_sent: dict[str, int] = {}
    recent_query_answers: dict[str, int] = {}
    route_evidence_settle_until_ms: dict[str, int] = {}
    route_evidence_settle_logged: set[str] = set()
    # Keep a selected route across local/API busy deferrals. A route is a
    # useful decision, not a one-loop hint; it is discarded only after its
    # evidence ages out or after a successful handoff.
    selected_route_cache: dict[str, tuple[RoutePlan, int, str]] = {}
    # A targeted QUERY CALL normally receives an answer within one or two
    # JS8Call cycles. Keep enough context for that response without blocking
    # discovery for several minutes; the fallback defer below is never shorter
    # than this window.
    query_context_window_ms = 90_000

    def queue_protocol_reply(
        text: str,
        peer: str,
        message_id: str = "",
        kind: str = "",
    ) -> None:
        # Deduplicate semantically.  Delivery receipts include a timestamp,
        # so hashing the wire text would allow duplicate receipt frames when
        # a sender retransmits the same message ID.
        cumulative_part_ack = kind.upper().startswith("PART_ACK:")
        key_material = f"{peer.upper()}\n{message_id.upper()}\n"
        key_material += "PART_ACK" if cumulative_part_ack else kind.upper()
        if not message_id or not kind:
            key_material += "\n" + text
        key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
        if cumulative_part_ack:
            # A newer bitmap supersedes an unsent older bitmap. This saves
            # airtime when multiple frames complete before the reply slot.
            pending_protocol_replies[key] = (utc_now_ms() + 5_000, text, peer.upper())
        else:
            pending_protocol_replies.setdefault(key, (utc_now_ms() + 5_000, text, peer.upper()))
        database.audit(
            "message.protocol_reply_queued",
            {"peer": peer.upper(), "message_id": message_id, "text_length": len(text)},
        )

    async def return_capability_for_collected_message(
        original_sender: str,
        immediate_source: str,
        message_id: str,
        incoming_path: tuple[str, ...],
    ) -> None:
        """Return a CAP toward the original sender after custodian retrieval."""
        local_call = str(status.get("callsign", "")).strip().upper()
        original = original_sender.strip().upper()
        source = immediate_source.strip().upper()
        if not local_call or not original or original == local_call:
            return
        now = utc_now_ms()
        if now - retrieval_capability_last_sent.get(original, 0) < 60 * 60 * 1000:
            database.audit(
                "delivery.control",
                {
                    "label": "JS8Mail discovery already reported",
                    "target": original,
                    "status": "suppressed",
                    "detail": "Capability return is rate-limited to once per hour.",
                    "message_id": message_id,
                },
            )
            return
        reverse_path = reverse_custody_path(local_call, original, incoming_path)
        selected_path = reverse_path
        if not selected_path:
            plan = service.plan_route(local_call, original, band=str(status.get("band", "")))
            if len(plan.path) >= 3:
                selected_path = tuple(plan.path)
        if len(selected_path) >= 3:
            text = format_relay_text(selected_path, format_capability())
        else:
            selected_path = (local_call, original)
            text = f"{original} {format_capability()}"
        path_text = "→".join(selected_path)
        detail = (
            f"Message collected from {source}; returning JS8Mail capability toward "
            f"the original sender."
        )
        if not reverse_path:
            detail += (
                " No recorded reverse path was available; this is a direct reachability attempt."
            )
        database.audit(
            "delivery.control",
            {
                "label": "JS8Mail discovery · delivery confirmation",
                "target": original,
                "status": "started",
                "detail": detail,
                "path": path_text,
                "message_id": message_id,
            },
        )
        try:
            await controller.send_rf(text)
        except (ConnectionError, OSError, RuntimeError, AirtimeBudgetExceeded) as exc:
            database.audit(
                "delivery.control",
                {
                    "label": "JS8Mail discovery · delivery confirmation",
                    "target": original,
                    "status": "deferred",
                    "detail": f"Waiting to return capability: {radio_exception_reason(exc)}.",
                    "path": path_text,
                    "message_id": message_id,
                },
            )
            return
        retrieval_capability_last_sent[original] = now
        database.audit(
            "delivery.control",
            {
                "label": "JS8Mail discovery · delivery confirmation",
                "target": original,
                "status": "submitted",
                "detail": "Message was collected from a custodian; CAP queued for the original sender.",
                "path": path_text,
                "message_id": message_id,
            },
        )

    # A compact QUERY CALL response does not repeat the queried callsign.
    # Restore very recent contexts so a daemon restart between query and
    # response does not discard an otherwise useful positive answer.
    query_context_now = utc_now_ms()
    for audit in database.recent_audit_events(
        "discovery.query_submitted", query_context_now - QUERY_RESPONSE_MAX_MS
    ):
        payload = audit["payload"]
        action = str(payload.get("action", ""))
        if action not in {"candidate_query_call", "allcall_query_call"}:
            continue
        text_fields = str(payload.get("text", "")).strip().upper().split()
        if len(text_fields) < 4 or text_fields[-2:] == ["QUERY", "CALL"]:
            continue
        destination = text_fields[-1].rstrip("?")
        responder = str(payload.get("target", "")).strip().upper()
        if not destination or not responder:
            continue
        key = (
            f"call-query:{destination}"
            if responder == "@ALLCALL"
            else f"candidate-query:{responder}:{destination}"
        )
        pending_call_queries.append(
            PendingCallQuery(
                int(audit["created_at_ms"]),
                destination,
                responder,
                key,
                str(payload.get("band", "")),
                QUERY_RESPONSE_MAX_MS,
            )
        )

    def apply_radio_context(params: dict[str, Any]) -> None:
        band, dial_frequency = context_from_params(params)
        if band:
            status["band"] = band
        if dial_frequency is not None:
            status["dial_frequency"] = dial_frequency

    async def submit_query(
        key: str,
        text: str,
        action: str,
        target: str,
        scheduler: QueryScheduler = query_scheduler,
        route_destination: str | None = None,
    ) -> bool:
        now = int(asyncio.get_running_loop().time() * 1000)
        if not client.connected or not scheduler.due(key, now):
            return False
        now_wall = utc_now_ms()
        pending_call_queries[:] = [
            query
            for query in pending_call_queries
            if now_wall - query.submitted_at_ms <= LATE_QUERY_CONTEXT_MS
        ]
        if route_destination is not None:
            responder = target.strip().upper()
            destination = route_destination.strip().upper()
            if responder == "@ALLCALL" and any(
                query.responder == "@ALLCALL"
                and query.destination != destination
                and now_wall - query.submitted_at_ms <= query.response_window_ms
                for query in pending_call_queries
            ):
                # A compact ALLCALL YES cannot identify which queried
                # destination it answers. Keep one outstanding ALLCALL
                # destination so a valid answer is never misrouted.
                return False
            if any(
                query.responder == responder and query.destination != destination
                for query in pending_call_queries
            ):
                # CALL YES does not repeat the destination. Keep at most one
                # outstanding destination per directed station/@ALLCALL.
                return False
        try:
            await controller.send_rf(text)
            database.audit(
                "discovery.query_submitted",
                {
                    "action": action,
                    "target": target,
                    "text": text,
                    "band": str(status.get("band", "")),
                },
            )
            scheduler.record(key, now)
            if route_destination is not None:
                response_window_ms = query_response_window_ms(action, status.get("speed", 0))
                pending_call_queries.append(
                    PendingCallQuery(
                        now_wall,
                        route_destination.strip().upper(),
                        target.strip().upper(),
                        key,
                        str(status.get("band", "")),
                        response_window_ms,
                    )
                )
                del pending_call_queries[:-16]
            return True
        except (ConnectionError, RuntimeError):
            scheduler.record(key, now)
            return False

    def queue_message_retrieval(custodian: str, stored_id: int, reason: str) -> None:
        """Queue a targeted QUERY MSG without blocking the RX event handler."""
        key = (custodian.strip().upper(), int(stored_id))
        if not key[0] or not 0 <= key[1] <= 2_147_483_647:
            return
        # JS8Call message IDs are local to a custodian but stable for the
        # lifetime of its persistent store. Once we have assembled that ID,
        # a repeated YES announcement must not make us fetch the same mail
        # every time the custodian answers a broad QUERY MSGS.
        if key in completed_retrievals:
            database.audit(
                "inbox.retrieval_duplicate_suppressed",
                {
                    "custodian": key[0],
                    "js8call_message_id": key[1],
                    "reason": reason,
                },
            )
            return
        if key in pending_retrievals:
            return
        pending_retrievals[key] = (utc_now_ms(), 0)
        database.audit(
            "inbox.retrieval_pending",
            {
                "custodian": key[0],
                "js8call_message_id": key[1],
                "attempt": 0,
                "reason": reason,
            },
        )

    async def service_pending_retrievals(now_wall_ms: int) -> None:
        """Submit queued retrievals during normal scheduler opportunities."""
        for key, (due_at_ms, submitted_count) in list(pending_retrievals.items()):
            if now_wall_ms < due_at_ms:
                continue
            custodian, stored_id = key
            if submitted_count >= max_retrieval_attempts:
                database.audit(
                    "inbox.retrieval_exhausted",
                    {
                        "custodian": custodian,
                        "js8call_message_id": stored_id,
                        "attempt": submitted_count,
                    },
                )
                pending_retrievals.pop(key, None)
                continue
            try:
                # Never let a stale JS8Call TX train hold the discovery loop
                # indefinitely.  The retrieval remains pending and will be
                # retried on the next scheduler opportunity.
                await asyncio.wait_for(
                    controller.send_rf(retrieve_message_query(custodian, stored_id)),
                    timeout=30,
                )
            except (TimeoutError, ConnectionError, OSError, RuntimeError, AirtimeBudgetExceeded) as exc:
                # Base the backoff on the end of the handoff attempt. A
                # timeout can consume the whole 30 seconds; using the loop's
                # old timestamp would make the item immediately eligible and
                # create a tight retry loop while JS8Call remains busy.
                pending_retrievals[key] = (utc_now_ms() + 30_000, submitted_count)
                database.audit(
                    "inbox.retrieval_failed",
                    {
                        "custodian": custodian,
                        "js8call_message_id": stored_id,
                        "attempt": submitted_count,
                        "reason": str(exc) or type(exc).__name__,
                    },
                )
                continue
            attempt = submitted_count + 1
            pending_retrievals[key] = (now_wall_ms + retrieval_retry_delay_ms, attempt)
            database.audit(
                "inbox.retrieval_submitted",
                {
                    "custodian": custodian,
                    "js8call_message_id": stored_id,
                    "attempt": attempt,
                },
            )

    async def discovery_loop() -> None:
        inbox_key = "inbox:broadcast"
        last_prune_at_ms = 0
        last_context_refresh_at_ms = 0
        while True:
            await asyncio.sleep(5)
            now_wall_ms = utc_now_ms()
            if now_wall_ms - last_prune_at_ms >= 60 * 60 * 1000:
                database.prune_observations(now_ms=now_wall_ms)
                database.prune_groups(now_ms=now_wall_ms)
                last_prune_at_ms = now_wall_ms
            if not client.connected or args.tx_mode != "automatic" or status.get("paused"):
                continue
            if now_wall_ms - last_context_refresh_at_ms >= 15_000:
                try:
                    frequency = await client.request_read_only("RIG.GET_FREQ")
                    apply_radio_context(dict(frequency.params))
                    if not status.get("dial_frequency") and frequency.value.strip().isdigit():
                        status["dial_frequency"] = int(frequency.value.strip())
                        status["band"] = band_from_frequency_hz(int(frequency.value.strip()))
                except (ConnectionError, OSError, RuntimeError):
                    pass
                last_context_refresh_at_ms = now_wall_ms
            await service_pending_retrievals(now_wall_ms)
            now = int(asyncio.get_running_loop().time() * 1000)
            if inbox_scheduler.due(inbox_key, now):
                await submit_query(
                    inbox_key,
                    messages_query(),
                    "messages_query",
                    "@ALLCALL",
                    scheduler=inbox_scheduler,
                )
            # A visible [JS8MAIL/x.y.z] marker is an invitation, not proof of
            # a feature set. Queue the CAP response rather than trying to send
            # it from inside the RX event handler; JS8Call may still be
            # finishing the received message at that moment.
            for peer, due_at in list(pending_capability_advertisements.items()):
                if now_wall_ms < due_at:
                    continue
                if (
                    str(status.get("pending_capability_peer") or "") == peer
                    or now_wall_ms - capability_last_sent.get(peer, 0)
                    < CAPABILITY_RESPONSE_COOLDOWN_MS
                ):
                    pending_capability_advertisements.pop(peer, None)
                    pending_capability_reasons.pop(peer, None)
                    continue
                try:
                    await controller.send_rf(f"{peer} {format_capability()}")
                except (ConnectionError, OSError, RuntimeError, AirtimeBudgetExceeded) as exc:
                    pending_capability_advertisements[peer] = now_wall_ms + 30_000
                    database.audit(
                        "peer.capability_advertisement_deferred",
                        {"peer": peer, "reason": str(exc) or type(exc).__name__},
                    )
                else:
                    pending_capability_advertisements.pop(peer, None)
                    reason = pending_capability_reasons.pop(peer, "explicit CAP handshake")
                    capability_last_sent[peer] = now_wall_ms
                    database.audit(
                        "peer.capability_advertisement_submitted",
                        {"peer": peer, "reason": reason},
                    )
            for reply_key, (due_at, reply_text, peer) in list(pending_protocol_replies.items()):
                if now_wall_ms < due_at:
                    continue
                try:
                    await controller.send_rf(reply_text)
                except (ConnectionError, OSError, RuntimeError, AirtimeBudgetExceeded) as exc:
                    pending_protocol_replies[reply_key] = (now_wall_ms + 30_000, reply_text, peer)
                    database.audit(
                        "message.protocol_reply_deferred",
                        {"peer": peer, "reason": str(exc) or type(exc).__name__},
                    )
                else:
                    pending_protocol_replies.pop(reply_key, None)
                    database.audit("message.protocol_reply_submitted", {"peer": peer})
            # Resolve one durable RF transaction at a time.  The parent
            # message is deliberately not used as the ACK correlation key:
            # it may have been moved back to route discovery after a timeout,
            # while the radio can still deliver a late ACK for the exact
            # transmission.
            for broadcast in database.expire_unfinished_broadcasts(now_wall_ms):
                broadcast_message = database.get_message(str(broadcast["message_id"]))
                if (
                    broadcast_message is not None
                    and broadcast_message["state"] == MessageState.IN_PROGRESS
                ):
                    database.record_attempt(
                        str(broadcast["message_id"]),
                        "group_broadcast",
                        str(broadcast["target"]),
                        "uncertain",
                        "RF completion was not observed; not rebroadcasting automatically",
                    )
                    database.transition_message(str(broadcast["message_id"]), MessageState.FAILED)
            for transaction in database.expire_transmission_transactions(now_wall_ms):
                message = database.get_message(str(transaction["message_id"]))
                if message is None or message["state"] in {
                    MessageState.STORED,
                    MessageState.DELIVERED,
                    MessageState.FAILED,
                    MessageState.EXPIRED,
                    MessageState.CANCELLED,
                }:
                    continue
                message_id = str(message["id"])
                operation = str(transaction["operation"])
                if operation == "store":
                    custodian = str(transaction["expected_responder"])
                    database.record_attempt(
                        message_id,
                        "store_timeout",
                        custodian,
                        "uncertain",
                        "no custodian ACK before response deadline; storage is unconfirmed",
                    )
                    database.upsert_custody(
                        message_id,
                        custodian,
                        "failed",
                        "no legacy JS8Call store ACK; message may still be stored",
                    )
                    if message["state"] == MessageState.IN_PROGRESS:
                        database.transition_message(message_id, MessageState.WAITING_ROUTE)
                    database.defer_message(
                        message_id,
                        LEGACY_CUSTODY_ALTERNATE_DISCOVERY_DELAY_MS,
                        "custodian ACK absent; discovering another route or custodian",
                        increment_retry=False,
                    )
                else:
                    database.record_attempt(
                        message_id,
                        "delivery_timeout",
                        str(transaction["target"]),
                        "uncertain",
                        "no ACK before the operation response deadline",
                    )
                    if message["state"] == MessageState.IN_PROGRESS:
                        database.transition_message(message_id, MessageState.WAITING_ROUTE)
                    database.defer_message(
                        message_id,
                        2 * 60 * 1000,
                        "delivery ACK absent; route discovery will try another opportunity",
                    )
            for message in database.list_messages():
                if message["state"] not in {
                    MessageState.QUEUED,
                    MessageState.IN_PROGRESS,
                    MessageState.WAITING_ROUTE,
                }:
                    continue
                destination = str(message["destination"])
                # Group traffic is a one-way broadcast, not a directed
                # delivery operation. Do this before route discovery so a
                # queued group post can never wait for an ACK or generate
                # QUERY CALL traffic for the group.
                if (
                    destination.startswith("@")
                    and message["state"]
                    in {
                        MessageState.QUEUED,
                        MessageState.WAITING_ROUTE,
                    }
                    and database.due_for_retry(str(message["id"]))
                ):
                    try:
                        await controller.transmit(str(message["id"]))
                    except AirtimeBudgetExceeded as exc:
                        if exc.scope == "per-message-total":
                            database.record_attempt(
                                str(message["id"]),
                                "group_broadcast",
                                destination,
                                "failed",
                                "per-message airtime budget exhausted",
                            )
                            database.transition_message(str(message["id"]), MessageState.FAILED)
                        else:
                            retry_at = exc.retry_at_ms
                            now_ms = utc_now_ms()
                            if retry_at is None or retry_at <= now_ms:
                                retry_delay = 60_000
                            else:
                                retry_delay = retry_at - now_ms
                            database.record_attempt(
                                str(message["id"]),
                                "group_broadcast",
                                destination,
                                "deferred",
                                "airtime budget exhausted; waiting for the next eligible window",
                            )
                            database.defer_message(
                                str(message["id"]),
                                retry_delay,
                                "group broadcast waiting for the next eligible airtime window",
                                increment_retry=False,
                            )
                    except (ConnectionError, OSError, RuntimeError, TypeError, ValueError) as exc:
                        database.record_attempt(
                            str(message["id"]),
                            "group_broadcast",
                            destination,
                            "deferred",
                            f"broadcast handoff unavailable: {radio_exception_reason(exc)}",
                        )
                        database.defer_message(
                            str(message["id"]),
                            60_000,
                            "group broadcast waiting for an available JS8Call TX slot",
                            increment_retry=False,
                        )
                    continue
                expires_at_ms = message.get("expires_at_ms")
                if isinstance(expires_at_ms, int) and expires_at_ms <= utc_now_ms():
                    database.transition_message(str(message["id"]), MessageState.EXPIRED)
                    database.record_attempt(
                        str(message["id"]), "expiry", destination, "expired", "retry window elapsed"
                    )
                    continue
                if message["state"] == MessageState.QUEUED:
                    # A queued message may be restored after a daemon restart
                    # or an operator retry without passing through the HTTP
                    # request that normally starts preparation.
                    try:
                        await controller.prepare(str(message["id"]))
                    except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
                        database.record_attempt(
                            str(message["id"]),
                            "prepare",
                            destination,
                            "deferred",
                            radio_exception_reason(exc),
                        )
                    continue
                if message["state"] == MessageState.IN_PROGRESS:
                    transactions = database.list_transmission_transactions(str(message["id"]))
                    active_transactions = [
                        item
                        for item in transactions
                        if item["status"] in {"queued", "tx_active", "awaiting_ack"}
                    ]
                    attempts = database.list_attempts(str(message["id"]))
                    enhanced_message = bool(
                        database.list_message_parts(
                            str(message["id"]),
                            direction="outgoing",
                            peer=destination,
                        )
                    )
                    direct_submissions = [
                        attempt
                        for attempt in attempts
                        if attempt["action"] in {"direct", "multipart"}
                        and attempt["status"] == "submitted"
                    ]
                    final_peer_ack_count = (
                        distinct_ack_count(attempts, destination) if enhanced_message else 0
                    )
                    if (
                        enhanced_message
                        and direct_submissions
                        and final_peer_ack_count >= ENHANCED_ACK_RETRY_LIMIT
                    ):
                        database.record_attempt(
                            str(message["id"]),
                            "receipt_unconfirmed",
                            destination,
                            "uncertain",
                            "JS8Call accepted the message, but no JS8Mail receipt arrived "
                            f"after {final_peer_ack_count} ACKs; automatic retries held",
                        )
                        database.hold_after_js8call_ack(str(message["id"]))
                        selected_route_cache.pop(str(message["id"]), None)
                        continue
                    if active_transactions:
                        # A new message or a discovery tick must never fill a
                        # receive window belonging to this transaction.
                        continue
                    if not any(item["status"] == "acknowledged" for item in transactions):
                        database.record_attempt(
                            str(message["id"]),
                            "reconcile",
                            str(message["destination"]),
                            "deferred",
                            "previous RF transaction ended without a durable ACK; returning to discovery",
                        )
                        database.transition_message(str(message["id"]), MessageState.WAITING_ROUTE)
                        database.defer_message(
                            str(message["id"]),
                            2 * 60 * 1000,
                            "previous transaction ended; retrying after a receive window",
                        )
                        continue
                direct_expired = False
                if message["state"] == MessageState.IN_PROGRESS:
                    has_followup = any(
                        (
                            attempt["action"] == "delivery_ack"
                            or (attempt["action"] == "standard_ack" and not enhanced_message)
                        )
                        and attempt["status"] in {"received", "confirmed"}
                        for attempt in attempts
                    )
                    if direct_submissions and not has_followup:
                        relevant_tx = next(
                            (
                                item
                                for item in reversed(transactions)
                                if item["operation"] in {"direct", "multipart"}
                                and item["tx_finished_at_ms"] is not None
                            ),
                            None,
                        )
                        last_direct = int(relevant_tx["tx_finished_at_ms"]) if relevant_tx else None
                        deadline_ms = (
                            ENHANCED_RECEIPT_DEADLINE_MS
                            if enhanced_message
                            else DIRECT_RESPONSE_DEADLINE_MS
                        )
                        direct_expired = (
                            last_direct is not None and now_wall_ms - last_direct >= deadline_ms
                        )
                        if direct_expired:
                            database.record_attempt(
                                str(message["id"]),
                                "direct_timeout",
                                destination,
                                "failed",
                                f"no final delivery evidence within {deadline_ms // 60_000}-minute post-TX deadline",
                            )
                            database.transition_message(
                                str(message["id"]), MessageState.WAITING_ROUTE
                            )
                            origin = str(status.get("callsign", "")).upper()
                            if origin:
                                try:
                                    speed = int(status.get("speed", 1))
                                except (TypeError, ValueError):
                                    speed = 0
                                database.record_link_outcome(
                                    origin,
                                    destination,
                                    speed if speed in SPEED_AIRTIME_MS else 0,
                                    None,
                                    False,
                                    str(status.get("band", "")),
                                )
                    # A relay-hop ACK proves custody of that hop, not final
                    # delivery. Do not retry during the short forwarding
                    # deadline, but do not leave the message permanently
                    # stuck in IN_PROGRESS if the relay never produces a
                    # final ACK/receipt either.
                    relay_hop_acks = [
                        attempt
                        for attempt in attempts
                        if attempt["action"] == "standard_ack"
                        and attempt["status"] == "received"
                        and str(attempt["target"]).upper() != destination.upper()
                    ]
                    if relay_hop_acks and not any(
                        attempt["action"] == "delivery_ack"
                        and attempt["status"] in {"received", "confirmed"}
                        for attempt in attempts
                    ):
                        last_hop_ack = int(relay_hop_acks[-1]["created_at_ms"])
                        if now_wall_ms - last_hop_ack >= DIRECT_RESPONSE_DEADLINE_MS:
                            database.record_attempt(
                                str(message["id"]),
                                "relay_forward_timeout",
                                destination,
                                "failed",
                                "hop acknowledged but no final delivery evidence arrived",
                            )
                            database.transition_message(
                                str(message["id"]), MessageState.WAITING_ROUTE
                            )
                            selected_route_cache.pop(str(message["id"]), None)
                            database.wake_message_for_route(str(message["id"]))
                            continue
                direct_age_ms = fresh_direct_response_age_ms(
                    service,
                    destination,
                    str(status.get("callsign", "")),
                    str(status.get("band", "")),
                    now_ms=now_wall_ms,
                )
                heard_age_ms = service.recent_heard_age_ms(
                    destination,
                    band=str(status.get("band", "")),
                )
                message_id = str(message["id"])
                cached_route = selected_route_cache.get(message_id)
                blocked_paths = service.blocked_message_paths(message_id, now_ms=now_wall_ms)
                retained_plan: RoutePlan | None = None
                if cached_route is not None:
                    candidate_plan, selected_at_ms, selected_band = cached_route
                    if (
                        selected_band == str(status.get("band", ""))
                        and now_wall_ms - selected_at_ms < ROUTE_HOP_PROBE_FRESH_MS
                        and candidate_plan.path not in blocked_paths
                    ):
                        retained_plan = candidate_plan
                    else:
                        selected_route_cache.pop(message_id, None)
                settle_until = route_evidence_settle_until_ms.get(message_id)
                if settle_until is not None and direct_age_ms is None:
                    if now_wall_ms < settle_until:
                        if message_id not in route_evidence_settle_logged:
                            database.record_attempt(
                                message_id,
                                "route_evidence_settling",
                                destination,
                                "waiting",
                                f"waiting {max(1, (settle_until - now_wall_ms) // 1000)}s for competing replies",
                            )
                            route_evidence_settle_logged.add(message_id)
                        continue
                    route_evidence_settle_until_ms.pop(message_id, None)
                    route_evidence_settle_logged.discard(message_id)
                    database.record_attempt(
                        message_id,
                        "route_evidence_settling",
                        destination,
                        "complete",
                        "reply collection window ended; selecting the best available path",
                    )
                if (
                    message["state"] == MessageState.WAITING_ROUTE
                    and (direct_age_ms is not None or heard_age_ms is not None)
                    and not direct_expired
                    and retained_plan is None
                    and database.due_for_retry(str(message["id"]))
                ):
                    if message["state"] == MessageState.WAITING_ROUTE:
                        if direct_age_ms is not None:
                            route_detail = f"recent direct response, {direct_age_ms // 1000}s ago"
                        else:
                            assert heard_age_ms is not None
                            route_detail = (
                                f"recently heard, {heard_age_ms // 1000}s ago; "
                                "likely reachable, not confirmed"
                            )
                        database.record_attempt(
                            str(message["id"]), "route", destination, "available", route_detail
                        )
                        origin = str(status.get("callsign", "")).upper()
                        retained_plan = RoutePlan(
                            RouteAction.DIRECT,
                            (origin, destination),
                            0.0,
                            0.0,
                            0,
                            f"Retaining direct route while evidence remains fresh ({route_detail}).",
                        )
                        selected_route_cache[message_id] = (
                            retained_plan,
                            now_wall_ms,
                            str(status.get("band", "")),
                        )
                        try:
                            future = asyncio.create_task(
                                controller.transmit(str(message["id"]), retained_plan)
                            )
                            await future
                            latest_attempt = database.list_attempts(message_id)[-1]
                            if latest_attempt["action"] != "capability_wait":
                                selected_route_cache.pop(message_id, None)
                        except (
                            ConnectionError,
                            OSError,
                            RuntimeError,
                            TypeError,
                            ValueError,
                        ) as exc:
                            if (
                                isinstance(exc, AirtimeBudgetExceeded)
                                and exc.scope == "per-message-total"
                            ):
                                database.record_attempt(
                                    str(message["id"]),
                                    "direct",
                                    destination,
                                    "failed",
                                    "one-hour per-message airtime ceiling reached",
                                )
                                database.transition_message(str(message["id"]), MessageState.FAILED)
                                continue
                            database.record_attempt(
                                str(message["id"]),
                                "direct",
                                destination,
                                "deferred",
                                f"local/API handoff deferred: {radio_exception_reason(exc)}",
                            )
                            receiving_message = "receiving a directed message" in str(exc)
                            retry_delay = 60_000
                            if receiving_message:
                                retry_delay = max(
                                    5_000,
                                    int(status.get("incoming_directed_until_ms", 0) or 0)
                                    - utc_now_ms(),
                                )
                            database.defer_message(
                                str(message["id"]),
                                retry_delay,
                                "route known but JS8Call is receiving directed mail"
                                if receiving_message
                                else "route known but JS8Call TX slot was still occupied",
                            )
                    continue
                # A query-call reply can complete a multi-hop path without
                # requiring the original destination to answer us directly.
                # Use that fresh evidence as soon as the message is due.
                if message["state"] == MessageState.WAITING_ROUTE and database.due_for_retry(
                    str(message["id"])
                ):
                    plan = retained_plan or service.plan_route(
                        str(status.get("callsign", "")),
                        destination,
                        attempted_paths=database.attempted_message_paths(str(message["id"])),
                        blocked_paths=blocked_paths,
                        band=str(status.get("band", "")),
                    )
                    if len(plan.path) >= 2:
                        if retained_plan is None:
                            database.record_attempt(
                                str(message["id"]),
                                "route",
                                destination,
                                "selected",
                                plan.explanation,
                            )
                            selected_route_cache[message_id] = (
                                plan,
                                now_wall_ms,
                                str(status.get("band", "")),
                            )
                        else:
                            database.record_attempt(
                                message_id,
                                "route",
                                destination,
                                "retained",
                                f"retained selected path while evidence remains fresh: {'→'.join(plan.path)}",
                            )
                        if not await controller.ensure_route_first_hop_reachable(
                            str(message["id"]), plan.path
                        ):
                            continue
                        try:
                            await controller.transmit(str(message["id"]), plan)
                            latest_attempt = database.list_attempts(message_id)[-1]
                            if latest_attempt["action"] != "capability_wait":
                                selected_route_cache.pop(message_id, None)
                        except (
                            ConnectionError,
                            OSError,
                            RuntimeError,
                            TypeError,
                            ValueError,
                        ) as exc:
                            if (
                                isinstance(exc, AirtimeBudgetExceeded)
                                and exc.scope == "per-message-total"
                            ):
                                database.record_attempt(
                                    str(message["id"]),
                                    "relay",
                                    plan.path[1],
                                    "failed",
                                    "one-hour per-message airtime ceiling reached",
                                )
                                database.transition_message(str(message["id"]), MessageState.FAILED)
                                continue
                            database.record_attempt(
                                str(message["id"]),
                                "relay",
                                plan.path[1],
                                "deferred",
                                f"route selected but TX was unavailable: {radio_exception_reason(exc)}",
                            )
                            reason = radio_exception_reason(exc)
                            retry_delay = 60_000
                            if "receiving a directed message" in reason:
                                retry_delay = max(
                                    5_000,
                                    int(status.get("incoming_directed_until_ms", 0) or 0)
                                    - utc_now_ms(),
                                )
                            elif "recently decoded RF activity" in reason:
                                retry_delay = max(
                                    5_000,
                                    int(status.get("incoming_activity_until_ms", 0) or 0)
                                    - utc_now_ms(),
                                )
                            database.defer_message(
                                str(message["id"]),
                                retry_delay,
                                f"selected route deferred: {reason}",
                            )
                        continue
                promising = service.promising_stations(
                    destination, band=str(status.get("band", ""))
                )[:3]
                # Custody offers are subject to the same durable retry
                # deadline as every other discovery action.  Without this
                # guard, a local/API handoff failure would fall through on
                # every five-second discovery tick and repeatedly offer the
                # same message to the same custodian, ignoring the defer
                # interval written below (and the longer post-timeout
                # backoff written by the transaction reaper).
                if message.get("retry_count", 0) >= 3 and database.due_for_retry(
                    str(message["id"])
                ):
                    active_custody = {
                        str(item["custodian"]).upper()
                        for item in database.list_custody(str(message["id"]))
                        if item["status"]
                        in {"offered", "accepted", "retrieval_pending", "forwarded"}
                    }
                    custody_history = {
                        str(item["custodian"]).upper()
                        for item in database.list_custody(str(message["id"]))
                    }
                    untried_custodians: list[str] = []
                    retry_custodians: list[str] = []
                    for candidate in promising:
                        candidate_key = candidate.upper()
                        if candidate_key == destination.upper() or candidate_key in active_custody:
                            continue
                        policy = database.legacy_store_offer_policy(
                            str(message["id"]),
                            candidate,
                            retry_cooldown_ms=LEGACY_CUSTODY_RETRY_COOLDOWN_MS,
                            max_automatic_offers=LEGACY_CUSTODY_MAX_AUTOMATIC_OFFERS,
                            quarantine_ms=LEGACY_CUSTODY_FAILURE_QUARANTINE_MS,
                        )
                        if policy["eligible"]:
                            if int(policy["automatic_offer_count"]) == 0:
                                untried_custodians.append(candidate)
                            else:
                                retry_custodians.append(candidate)
                    # Prefer a custodian that has not seen this message yet.
                    # A second offer is still allowed when no new candidate is
                    # known, but it should not crowd out route diversity.
                    candidate_custodian = next(
                        iter(untried_custodians or retry_custodians), None
                    )
                    # Legacy custodians do not provide a portable end-to-end
                    # receipt. Limit the number of distinct offers and never
                    # offer a second custodian while an earlier one is still
                    # pending or accepted. A submitted offer without an ACK is
                    # also retained in durable history, so repeated failures
                    # quarantine that custodian instead of creating a loop.
                    if (
                        candidate_custodian is not None
                        and len(custody_history) < 3
                    ):
                        try:
                            await controller.transmit_store(str(message["id"]), candidate_custodian)
                        except (
                            ConnectionError,
                            OSError,
                            RuntimeError,
                            TypeError,
                            ValueError,
                        ) as exc:
                            if (
                                isinstance(exc, AirtimeBudgetExceeded)
                                and exc.scope == "per-message-total"
                            ):
                                database.record_attempt(
                                    str(message["id"]),
                                    "store",
                                    candidate_custodian,
                                    "failed",
                                    "one-hour per-message airtime ceiling reached",
                                )
                                database.transition_message(str(message["id"]), MessageState.FAILED)
                                continue
                            database.record_attempt(
                                str(message["id"]),
                                "store",
                                candidate_custodian,
                                "deferred",
                                radio_exception_reason(exc),
                            )
                            database.defer_message(
                                str(message["id"]), 60_000, "custodian offer unavailable"
                            )
                        continue
                    # A previously offered custodian may be cooling down or
                    # have reached the automatic limit. That restriction is
                    # local to that custodian: do not wait for its cooldown,
                    # because the destination may become reachable through a
                    # different route while the message is still active.
                if message["state"] == MessageState.WAITING_ROUTE and not database.due_for_retry(
                    str(message["id"])
                ):
                    # A defer deadline is authoritative even when fresh
                    # indirect evidence exists. QueryScheduler controls when
                    # the next targeted query is allowed; do not append a new
                    # message defer every five-second loop iteration.
                    continue

                # The RX handler and this loop run concurrently. A directed
                # SNR/YES response may have arrived after the first evidence
                # snapshot above, while this message was still due for
                # discovery. Re-read the database immediately before the
                # query handoff: a fresh direct response is stronger than any
                # pending fallback query and must wake the direct-send path.
                latest_direct_age_ms = fresh_direct_response_age_ms(
                    service,
                    destination,
                    str(status.get("callsign", "")),
                    str(status.get("band", "")),
                    now_ms=now_wall_ms,
                )
                if latest_direct_age_ms is not None:
                    database.wake_message_for_route(str(message["id"]))
                    continue

                call_key = f"call-query:{destination}"
                query_submitted = False
                query_wait_ms = query_context_window_ms
                if query_scheduler.due(call_key, now):
                    candidates = promising
                    if candidates:
                        for candidate in candidates:
                            candidate_key = f"candidate-query:{candidate}:{destination}"
                            if query_scheduler.due(candidate_key, now):
                                candidate_submitted = await submit_query(
                                    candidate_key,
                                    f"{candidate} QUERY CALL {destination}",
                                    "candidate_query_call",
                                    candidate,
                                    route_destination=destination,
                                )
                                database.record_attempt(
                                    str(message["id"]),
                                    "candidate_query_call",
                                    candidate,
                                    "submitted" if candidate_submitted else "blocked",
                                    destination,
                                )
                                query_submitted = query_submitted or candidate_submitted
                                if candidate_submitted:
                                    query_wait_ms = max(
                                        query_wait_ms,
                                        query_response_window_ms(
                                            "candidate_query_call", status.get("speed", 0)
                                        ),
                                    )
                    else:
                        allcall_submitted = await submit_query(
                            call_key,
                            call_query(destination),
                            "allcall_query_call",
                            "@ALLCALL",
                            route_destination=destination,
                        )
                        database.record_attempt(
                            str(message["id"]),
                            "allcall_query_call",
                            "@ALLCALL",
                            "submitted" if allcall_submitted else "blocked",
                            destination,
                        )
                        query_submitted = allcall_submitted
                        if allcall_submitted:
                            query_wait_ms = query_response_window_ms(
                                "allcall_query_call", status.get("speed", 0)
                            )
                delay_ms = min(
                    60_000 * (2 ** min(int(message.get("retry_count", 0)), 8)),
                    21_600_000,
                )
                defer_detail = (
                    f"query submitted; awaiting response for up to "
                    f"{query_wait_ms // 1000} seconds; "
                    f"fallback discovery in "
                    f"{max(delay_ms, query_wait_ms) // 1000} seconds"
                    if query_submitted
                    else f"no current route; discovery will retry in {delay_ms // 60000} minute(s)"
                )
                database.defer_message(
                    str(message["id"]),
                    max(delay_ms, query_wait_ms) if query_submitted else delay_ms,
                    defer_detail,
                )

    async def discovery_supervisor() -> None:
        """Keep discovery alive and make unexpected failures auditable."""
        while True:
            try:
                await discovery_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - scheduler must survive unexpected adapter/data errors
                database.audit(
                    "discovery.loop_error",
                    {"error": type(exc).__name__, "detail": str(exc)[:160]},
                )
                await asyncio.sleep(1)

    discovery_task = asyncio.create_task(discovery_supervisor())
    try:
        while True:
            try:
                await client.connect()
                status["connected"] = True
                database.audit("js8call.connected", {"host": args.host, "port": args.port})
                delay = 1.0

                # RX.ACTIVITY is a short display stream.  It may contain a
                # long directed message split across several events, while
                # unrelated QSOs arrive between those events.  Keep multiple
                # conservative streams; RX.DIRECTED remains authoritative.
                activity_assembler = ActivityAssembler()
                # CAP responses are also emitted as RX.ACTIVITY fragments,
                # but their first fragment is typically just
                # ``SOURCE: DESTINATION`` (without MSG). Keep a separate
                # control stream so a multi-frame CAP can complete even when
                # ordinary mail and unrelated QSOs are interleaved.
                capability_assembler = ActivityAssembler(accept_control_starts=True)

                async def handle(
                    event: NormalizedEvent,
                    activity_assembler: ActivityAssembler = activity_assembler,
                    capability_assembler: ActivityAssembler = capability_assembler,
                ) -> None:
                    nonlocal tx_settle_task
                    apply_radio_context(dict(event.params))
                    reassembled_activity_event = False
                    if event.event_type in {"RX.ACTIVITY", "RX.DIRECTED", "RX.SPOT", "RX.DECODE"}:
                        # The event can be delivered after the corresponding
                        # RF slot. Hold autonomous TX long enough to preserve
                        # a useful reply window, even before directed-message
                        # classification has completed. Start a new hold only
                        # after a genuine quiet gap; repeated unrelated
                        # decodes cannot starve the scheduler indefinitely.
                        now_activity_ms = utc_now_ms()
                        last_activity_ms = int(status.get("last_rx_activity_ms", 0) or 0)
                        status["incoming_activity_until_ms"], status["rx_activity_guard_slots"] = update_rx_activity_guard(
                            last_activity_ms,
                            int(status.get("rx_activity_guard_slots", 0) or 0),
                            now_activity_ms,
                            int(status.get("incoming_activity_until_ms", 0) or 0),
                        )
                        status["last_rx_activity_ms"] = now_activity_ms
                    if event.event_type == "RX.ACTIVITY":
                        bits = event.params.get("BITS")
                        try:
                            bits = int(bits) if bits is not None else None
                        except (TypeError, ValueError):
                            bits = None

                        def numeric_param(name: str) -> int | None:
                            value = event.params.get(name)
                            try:
                                return int(value) if value is not None else None
                            except (TypeError, ValueError):
                                return None

                        now_ms = utc_now_ms()
                        fragment = ActivityFragment(
                            str(event.value),
                            bits,
                            event.received_at_ms,
                            now_ms,
                            str(status.get("band", "")),
                            numeric_param("DIAL_FREQUENCY") or status.get("dial_frequency"),
                            numeric_param("OFFSET"),
                            numeric_param("SPEED"),
                        )
                        capability_assemblies = capability_assembler.feed(
                            # CAP advertisements may be heard between two
                            # other stations. Reassemble those too so this
                            # station can learn their capability passively;
                            # the normal handler below still limits replies
                            # to CAPs addressed to our callsign.
                            fragment,
                            local_destination=None,
                        )
                        for capability_assembly in capability_assemblies:
                            if not capability_assembly.complete or capability_assembly.ambiguous:
                                continue
                            control_text = extract_js8mail_control(capability_assembly.text)
                            if control_text is None:
                                continue
                            capability = find_capability(control_text)
                            control_ack = parse_ack(control_text)
                            if capability is None and control_ack is None:
                                continue
                            control_source = capability_assembly.source.upper()
                            control_destination = capability_assembly.destination.upper()
                            # A bare control train has no address prefix in
                            # some JS8Call activity streams. For a receipt or
                            # part ACK, the correlated outgoing message and
                            # its validated path provide a safe peer hint.
                            if not control_source and control_ack is not None:
                                correlated = database.get_message(control_ack[1])
                                if correlated is not None:
                                    control_source = str(correlated["destination"]).upper()
                                elif control_ack[0] == "delivered":
                                    metadata = parse_delivery_ack(control_text)
                                    if metadata is not None and metadata[2] != ("?",):
                                        control_source = metadata[2][-1].upper()
                            if not control_destination:
                                control_destination = str(status.get("callsign", "")).upper()
                            if not control_source:
                                # An overheard bare control frame is useful
                                # only when its endpoint is explicit. Do not
                                # guess a peer from an incomplete display line.
                                continue
                            # Re-enter the normal directed-frame path. This
                            # keeps capability learning, response throttling,
                            # pending-message wakeup, receipt reconciliation,
                            # and audit behavior identical for RX.DIRECTED and
                            # reconstructed activity/control trains.
                            cap_params = dict(event.params)
                            cap_params.update(
                                {
                                    "FROM": control_source,
                                    "TO": control_destination,
                                    "CMD": "",
                                    "TEXT": control_text,
                                }
                            )
                            event = NormalizedEvent(
                                "RX.DIRECTED",
                                control_text,
                                cap_params,
                                event.received_at_ms,
                            )
                            reassembled_activity_event = True
                            break
                        assemblies = activity_assembler.feed(
                            fragment,
                            local_destination=str(status.get("callsign", "")),
                        )
                        for assembly in assemblies:
                            source = assembly.source.upper()
                            destination = assembly.destination.upper()
                            assembled = assembly.text
                            body = re.sub(
                                r"^\s*[A-Z0-9/]{1,16}\s*:\s*[A-Z0-9/]{1,16}\s+MSG\s*",
                                "",
                                assembled,
                                count=1,
                                flags=re.IGNORECASE,
                            )
                            body = ActivityAssembler.clean_text(body)
                            if body and not assembly.complete:
                                # Reuse a matching partial already persisted
                                # by an earlier connection. This makes a
                                # daemon restart mid-message safe: the
                                # continued activity updates one inbox item
                                # instead of creating a second copy.
                                partial_id = database.find_partial_inbox(source, body)
                                if partial_id is None:
                                    partial_id = (
                                        "legacy-partial-"
                                        + hashlib.sha256(
                                            f"{source}\n{destination}\n{body[:96]}".encode()
                                        ).hexdigest()[:16]
                                    )
                                database.upsert_inbox_message(
                                    source,
                                    partial_id,
                                    body,
                                    1,
                                    (),
                                    False,
                                    (source,),
                                    protocol="js8m"
                                    if "J8M1 D " in assembled.upper()
                                    else "standard",
                                    delivery="direct",
                                )
                                if assembly.ambiguous or assembly.gap_suspected:
                                    database.audit(
                                        "radio.activity_reassembly_uncertain",
                                        {
                                            "source": source,
                                            "destination": destination,
                                            "stream_id": assembly.stream_id,
                                            "ambiguous": assembly.ambiguous,
                                            "gap_suspected": assembly.gap_suspected,
                                        },
                                    )
                            if assembly.complete and not assembly.ambiguous:
                                complete_text = ActivityAssembler.clean_text(assembled)
                                synthetic_params = dict(event.params)
                                synthetic_params.update(
                                    {
                                        "FROM": source,
                                        "TO": destination,
                                        "CMD": "MSG",
                                        "TEXT": complete_text,
                                        "PARTIAL": False,
                                    }
                                )
                                event = NormalizedEvent(
                                    "RX.DIRECTED",
                                    complete_text,
                                    synthetic_params,
                                    event.received_at_ms,
                                )
                                reassembled_activity_event = True
                                database.audit(
                                    "radio.activity_reassembled",
                                    {
                                        "source": source,
                                        "destination": destination,
                                        "protocol": "js8m"
                                        if "J8M1 D " in assembled.upper()
                                        else "standard",
                                        "confidence": assembly.confidence,
                                    },
                                )
                                break
                    # PTT may drop briefly between frames of one message.
                    # Keep the transaction alive until a whole RF train has
                    # stayed quiet; TX.FRAME cancels a premature end timer.
                    if event.event_type == "RIG.PTT":
                        ptt = event.params.get("PTT")
                        ptt_on = ptt is True or str(event.value).lower() == "on"
                        status["radio_activity"] = "TX" if ptt_on else "RX"
                        tx_train.ptt(ptt_on, utc_now_ms())
                        active_transaction_id = controller.active_transaction_id
                        if ptt_on:
                            status["incoming_activity_until_ms"] = 0
                            status["last_rx_activity_ms"] = 0
                            status["rx_activity_guard_slots"] = 0
                            status["tx_train_pending"] = True
                            if tx_settle_task is not None:
                                tx_settle_task.cancel()
                                tx_settle_task = None
                        if ptt_on and active_transaction_id is not None:
                            database.mark_transmission_active_by_id(active_transaction_id)
                        if not ptt_on and tx_train.saw_ptt:
                            if tx_settle_task is not None:
                                tx_settle_task.cancel()
                            tx_settle_task = asyncio.create_task(settle_rf_train())
                    elif event.event_type.startswith("TX"):
                        # TX.FRAME can arrive after PTT has fallen. In that
                        # case keep the radio in RX while extending the
                        # completion guard for the just-finished frame.
                        if tx_train.ptt_on or not tx_train.saw_ptt:
                            status["radio_activity"] = "TX"
                        if event.event_type == "TX.FRAME":
                            tx_train.frame(utc_now_ms())
                            status["tx_train_pending"] = True
                            if tx_settle_task is not None:
                                tx_settle_task.cancel()
                                tx_settle_task = None
                            if tx_train.saw_ptt and not tx_train.ptt_on:
                                tx_settle_task = asyncio.create_task(settle_rf_train())
                    # The documented TCP API does not currently expose a
                    # generic "decode cycle finished" event. RX result events
                    # are therefore the strongest portable DCD evidence. The
                    # aliases below also support builds which forward the
                    # internal decode-complete notification.
                    decode_events = {
                        "RX.ACTIVITY",
                        "RX.DIRECTED",
                        "RX.SPOT",
                        "RX.DECODE",
                        "RX.DECODE_FINISHED",
                        "RX.DCD",
                        "DECODE.FINISHED",
                    }
                    if event.event_type in decode_events:
                        status["dcd_until_ms"] = utc_now_ms() + 1_000
                    if "J8M" in event.value.upper() or "JS8MAIL" in event.value.upper():
                        status["js8_activity_until_ms"] = utc_now_ms() + 1_000
                    database.record_observation(
                        event,
                        band=str(status.get("band", "")),
                        dial_frequency=status.get("dial_frequency"),
                    )
                    database.record_link_projection(
                        event,
                        band=str(status.get("band", "")),
                        dial_frequency=status.get("dial_frequency"),
                    )
                    # Only radio traffic is evidence that a group was heard.
                    # STATION.STATUS contains JS8Call's locally selected group
                    # and must not make an unobserved group look active.
                    if event.event_type.startswith(("RX.", "TX.")):
                        group_values = [event.value]
                        for key in ("TEXT", "TO"):
                            value = event.params.get(key)
                            if isinstance(value, str):
                                group_values.append(value)
                        for group in extract_groups(*group_values):
                            database.observe_group(group, default_group_description(group))
                    frame = (
                        normalize_directed_event(event)
                        if event.event_type.startswith("RX.DIRECTED")
                        else None
                    )
                    local_call = str(status.get("callsign", "")).upper()
                    # Some JS8Call builds mirror an RX.ACTIVITY fragment as
                    # RX.DIRECTED and attach BITS to it.  It is not a second
                    # authoritative message: feeding it through the normal
                    # mailbox path creates synthetic duplicates such as
                    # ``MSG<continuation>`` beside the real RX.DIRECTED
                    # message.  Keep the observation/link evidence, while
                    # letting the activity assembler own this fragment.
                    activity_mirror = (
                        not reassembled_activity_event
                        and event.event_type == "RX.DIRECTED"
                        and event.params.get("BITS") is not None
                        and bool(
                            re.match(
                                r"^\s*[A-Z0-9/]{1,16}\s*:\s*[A-Z0-9/]{1,16}",
                                str(event.value),
                                re.IGNORECASE,
                            )
                        )
                    )
                    if activity_mirror:
                        database.audit(
                            "radio.activity_mirror_ignored",
                            {"source": frame.source if frame is not None else ""},
                        )
                        return
                    directed_to_local = frame is not None and frame.destination in {
                        local_call,
                        "@ALLCALL",
                    }
                    if frame is None and event.event_type == "RX.ACTIVITY" and local_call:
                        activity_text = str(event.params.get("TEXT", event.value))
                        directed_to_local = bool(
                            re.match(r"^\s*[A-Z0-9/]{1,16}\s*:\s*", activity_text, re.IGNORECASE)
                            and re.search(
                                rf"\b{re.escape(local_call)}\b", activity_text, re.IGNORECASE
                            )
                        )
                    if directed_to_local:
                        # JS8Call deliberately suppresses automatic
                        # replies while a directed message is arriving.
                        # JS8Mail must apply the same rule to its own
                        # scheduler so a queued broadcast cannot occupy
                        # the next slot and truncate the incoming mail.
                        raw_directed = str(event.params.get("TEXT", event.value))
                        has_eot = bool(re.search(r"[♢◊]\s*$", raw_directed))
                        # JS8Call can expose a partial directed decode with
                        # both its continuation marker and the frame EOT
                        # (for example ``…… ♢``).  The EOT alone therefore
                        # is not proof that the incoming message is safe
                        # to interrupt with an automated transmission.
                        partial_marker = bool(re.search(r"(?:…|\.{3,})\s*[♢◊]?\s*$", raw_directed))
                        complete_directed = has_eot and not partial_marker
                        now = utc_now_ms()
                        # RX.ACTIVITY and RX.DIRECTED can be emitted for
                        # the same frame in either task order. Once the
                        # completed RX.DIRECTED event has been seen, do
                        # not let the activity copy reopen the partial
                        # message hold.
                        completion_guard = int(
                            status.get("incoming_directed_completion_guard_until_ms", 0) or 0
                        )
                        if event.event_type != "RX.ACTIVITY" or completion_guard <= now:
                            status["incoming_directed_until_ms"] = now + (
                                45_000 if complete_directed else 3 * 60_000
                            )
                        if complete_directed:
                            status["incoming_directed_completion_guard_until_ms"] = now + 45_000
                    ack = parse_ack(frame.payload) if frame is not None else None
                    source_value = frame.source if frame is not None else event.params.get("FROM")
                    source = source_value if isinstance(source_value, str) else ""
                    command = frame.command if frame is not None else ""
                    message_text = frame.payload if frame is not None else ""
                    resend = parse_resend_request(frame.payload) if frame is not None else None
                    if resend is not None and isinstance(source, str):
                        request_id, total, missing = resend
                        requested_message = database.get_message(request_id)
                        authorized = False
                        if requested_message is not None:
                            authorized = (
                                str(requested_message["destination"]).upper() == source.upper()
                                or any(
                                    item["custodian"].upper() == source.upper()
                                    and item["status"]
                                    in {"accepted", "retrieval_pending", "forwarded"}
                                    for item in database.list_custody(request_id)
                                )
                                or any(
                                    source.upper() in {call.upper() for call in path}
                                    for path in database.message_paths(request_id)
                                )
                            )
                        if authorized:
                            try:
                                if requested_message is None:
                                    raise ValueError("unknown multipart message")
                                parts = split_human_message(
                                    request_id, str(requested_message["body"])
                                )
                                if total != len(parts):
                                    raise ValueError(
                                        "multipart request total does not match stored message"
                                    )
                                raw_path = str(event.params.get("PATH", ""))
                                request_path = tuple(
                                    item.upper() for item in raw_path.split(">") if item
                                )
                                for number in missing:
                                    if number <= len(parts):
                                        payload = format_human_data_part(
                                            parts[number - 1],
                                            str(status.get("callsign", "")).upper(),
                                            str(requested_message["destination"]).upper(),
                                            str(requested_message.get("subject", "")),
                                        )
                                        text = (
                                            format_relay_message(request_path, payload)
                                            if len(request_path) >= 3
                                            else f"{source} {payload}"
                                        )
                                        await controller.send_rf(text, request_id)
                                database.record_attempt(
                                    request_id,
                                    "part_resend",
                                    source,
                                    "submitted",
                                    f"served {len(missing)} requested part(s) through custody path",
                                )
                            except (ValueError, RuntimeError, ConnectionError):
                                database.record_attempt(
                                    request_id,
                                    "part_resend",
                                    source,
                                    "failed",
                                    "unable to serve request",
                                )
                    availability_text = (
                        frame.wire_text
                        if frame is not None
                        else str(event.params.get("TEXT", event.value))
                    )
                    availability = parse_messages_available_context(availability_text)
                    if availability is not None:
                        announced_source, announced_destination, available_id = availability
                        responder = (
                            source.upper()
                            if isinstance(source, str) and source
                            else announced_source
                        )
                        if not responder:
                            responder = str(event.params.get("FROM", "")).strip().upper() or None
                        destination = (
                            frame.destination
                            if frame is not None
                            else announced_destination
                        )
                        if not destination:
                            destination = str(event.params.get("TO", "")).strip().upper() or None
                        if (
                            responder
                            and destination
                            and destination.upper()
                            in {local_call, "@ALLCALL"}
                        ):
                            queue_message_retrieval(
                                responder,
                                available_id,
                                "JS8Call reported a stored message in response to QUERY MSGS",
                            )
                    capability_text = frame.payload if frame is not None else str(event.value)
                    capability = (
                        parse_capability(capability_text)
                        if frame is not None
                        else find_capability(capability_text)
                    )
                    capability_source = source
                    if capability_source is None and event.event_type == "RX.ACTIVITY":
                        source_match = re.match(
                            r"^\s*([A-Z0-9/]{1,16})\s*:", capability_text, re.IGNORECASE
                        )
                        capability_source = (
                            source_match.group(1) if source_match is not None else None
                        )
                    # Only a CAP addressed to this station gets a response.
                    # CAPs overheard between other stations, including group
                    # traffic, are useful graph intelligence but must remain
                    # passive to avoid a broadcast response storm.
                    capability_reply_allowed = (
                        str(event.params.get("TO", "")).upper() == local_call
                        if frame is None
                        else frame.destination == local_call
                    )
                    if capability is not None and isinstance(capability_source, str):
                        version, features = capability
                        capability_now = utc_now_ms()
                        database.upsert_peer_capabilities(
                            capability_source, version, features, capability_now + CAPABILITY_TTL_MS
                        )
                        pending_capability_advertisements.pop(capability_source.upper(), None)
                        pending_capability_reasons.pop(capability_source.upper(), None)
                        for pending_message in database.list_messages(MessageState.WAITING_ROUTE):
                            if (
                                str(pending_message["destination"]).upper()
                                == capability_source.upper()
                            ):
                                database.wake_message_for_route(str(pending_message["id"]))
                        # CAP is a request/response hint, not an endlessly
                        # echoed heartbeat. One reply per peer per hour is
                        # enough to establish capability and prevents loops.
                        last_capability = capability_last_sent.get(capability_source.upper(), 0)
                        if capability_now - last_capability < 60 * 60 * 1000:
                            capability = None
                        else:
                            # Record the throttle only after the response is
                            # actually handed to JS8Call. A failed handoff
                            # must remain retryable rather than suppressing
                            # the peer's only capability response for an hour.
                            pass
                    if (
                        capability is not None
                        and isinstance(capability_source, str)
                        and capability_reply_allowed
                    ):
                        # Defer the response until JS8Call has completed the
                        # current RX event. Direct handoff here races the
                        # modem's directed-message acknowledgement path.
                        capability_peer = capability_source.upper()
                        pending_capability_advertisements[capability_peer] = utc_now_ms() + 15_000
                        pending_capability_reasons[capability_peer] = "explicit CAP handshake"
                        database.audit(
                            "peer.capability_ack_queued",
                            {"peer": capability_peer, "version": version},
                        )
                    # A group-directed MSG is useful alert traffic even when
                    # no JS8Mail peer is present. Preserve it in the separate
                    # group-alert inbox; @ALLCALL is deliberately excluded
                    # because ordinary CQ/query traffic is not mail.
                    if (
                        frame is not None
                        and isinstance(source, str)
                        and command == "MSG"
                        and frame.destination.startswith("@")
                        and frame.destination != "@ALLCALL"
                        and message_text.strip()
                    ):
                        group_id = (
                            "group-"
                            + hashlib.sha256(
                                f"{frame.destination}\n{source.upper()}\n{message_text}".encode()
                            ).hexdigest()[:16]
                        )
                        database.upsert_inbox_message(
                            source,
                            group_id,
                            message_text.strip(),
                            1,
                            (1,),
                            True,
                            tuple(str(event.params.get("PATH", source)).split(">")),
                            frame.destination,
                            delivery="group_broadcast",
                        )
                    # Legacy JS8Call messages arrive without a JS8Mail ID.
                    # Store them too, using a deterministic local fingerprint
                    # so repeated custodian retrieval does not create copies.
                    local_call = str(status.get("callsign", "")).upper()
                    if (
                        isinstance(source, str)
                        and command in {"MSG", "MSG TO:"}
                        and message_text.strip()
                        and not message_text.startswith("J8M1 ")
                        and not is_js8mail_wire_frame(message_text)
                        and frame is not None
                        and frame.destination == local_call
                    ):
                        if (
                            command == "MSG TO:"
                            and frame.stored_recipient.upper()
                            not in {
                                local_call,
                                "",
                            }
                            and not frame.stored_recipient.startswith("@")
                        ):
                            database.audit(
                                "custody.inbound_accepted",
                                {
                                    "custodian": local_call,
                                    "recipient": frame.stored_recipient,
                                    "sender": source.upper(),
                                },
                            )
                            # JS8Call also persists this in its own store. It
                            # is not an operator inbox message for us.
                            message_text = ""
                        if not message_text:
                            return
                        else:
                            marker_seen = contains_js8mail_marker(message_text)
                            # The readable version marker is transport
                            # metadata. Keep it in observations, but do not
                            # make it part of the operator's mailbox body.
                            message_text = clean_user_message(message_text)
                            if not message_text:
                                return
                            collected = any(key[0] == source.upper() for key in pending_retrievals)
                            original_sender = source
                            if collected:
                                # JS8Call's stored-message response normally
                                # preserves the origin in its structured
                                # fields or as an origin-prefixed MSG line.
                                # Prefer that over the immediate custodian.
                                for field in (
                                    "ORIGINAL_SENDER",
                                    "ORIGINAL_FROM",
                                    "ORIGIN",
                                ):
                                    candidate = str(event.params.get(field, "")).strip().upper()
                                    if re.fullmatch(r"[A-Z0-9/]{1,16}", candidate or ""):
                                        original_sender = candidate
                                        break
                                leading_origin = re.match(
                                    r"^([A-Z0-9/]{1,16})\s+MSG(?:\s+TO:\s*[^\s]+)?\s+(.*)$",
                                    message_text,
                                    re.IGNORECASE,
                                )
                                if leading_origin is not None:
                                    original_sender = leading_origin.group(1).upper()
                                    message_text = leading_origin.group(2).strip()
                            retrieved = re.search(
                                r"\s+FROM\s+([A-Z0-9/]{1,16})\s*$",
                                message_text,
                                re.IGNORECASE,
                            )
                            if retrieved is not None:
                                original_sender = retrieved.group(1).upper()
                                message_text = message_text[: retrieved.start()].rstrip()
                        partial = not frame.final or bool(
                            re.search(r"(?:…|\.{3,})\s*$", message_text)
                        )
                        partial_id = database.find_partial_inbox(source, message_text)
                        partial_match = None
                        if partial_id is None and not partial:
                            partial_match = database.find_partial_inbox_any_sender(message_text)
                            if partial_match is not None:
                                partial_sender, partial_id = partial_match
                                if partial_sender.upper() != source.upper():
                                    original_sender = partial_sender
                        legacy_id = partial_id or (
                            "legacy-partial-"
                            + hashlib.sha256(
                                f"{original_sender.upper()}\n{message_text.rstrip('… .')}".encode()
                            ).hexdigest()[:16]
                            if partial
                            else "legacy-"
                            + hashlib.sha256(
                                f"{original_sender.upper()}\n{message_text}".encode()
                            ).hexdigest()[:16]
                        )
                        inbox_path = tuple(str(event.params.get("PATH", source)).split(">"))
                        if partial_match is not None and partial_match[0].upper() != source.upper():
                            immediate_path = tuple(
                                item.strip().upper() for item in inbox_path if item.strip()
                            )
                            inbox_path = (partial_match[0].upper(),) + tuple(
                                item for item in immediate_path if item != partial_match[0].upper()
                            )
                        database.upsert_inbox_message(
                            original_sender,
                            legacy_id,
                            message_text.strip(),
                            1,
                            () if partial else (1,),
                            not partial,
                            inbox_path,
                            frame.stored_recipient if command == "MSG TO:" else "",
                            delivery="stored_collected" if collected else "direct",
                        )
                        if collected and original_sender.upper() != local_call:
                            incoming_path = tuple(
                                item.upper()
                                for item in str(event.params.get("PATH", source)).split(">")
                                if item.strip()
                            )
                            await return_capability_for_collected_message(
                                original_sender,
                                source,
                                legacy_id,
                                incoming_path,
                            )
                        # A readable version marker identifies a JS8Mail
                        # sender but does not claim a feature set. Keep the
                        # observation and, for a complete direct first-contact
                        # message, invite the sender into a delayed CAP
                        # exchange. The scheduler performs the response after
                        # JS8Call's ordinary ACK/receive opportunity has
                        # settled, so this cannot pre-empt the incoming mail.
                        if marker_seen:
                            marker_peer = local_call if collected else source.upper()
                            if marker_peer and marker_peer != local_call:
                                database.audit(
                                    "peer.js8mail_marker_observed",
                                    {
                                        "peer": marker_peer,
                                        "source": source.upper(),
                                        "collected": collected,
                                        "reason": "readable JS8Mail marker; passive evidence only",
                                    },
                                )
                            queue_marker_capability_response(
                                database,
                                pending_capability_advertisements,
                                pending_capability_reasons,
                                capability_last_sent,
                                source,
                                local_call,
                                marker_seen=marker_seen,
                                message_complete=not partial,
                                collected=collected,
                                addressed_to_local=(
                                    frame is not None and frame.destination == local_call
                                ),
                                pending_capability_peer=str(
                                    status.get("pending_capability_peer") or ""
                                ),
                            )
                        matching_retrievals = [
                            (retrieval_key, state)
                            for retrieval_key, state in pending_retrievals.items()
                            if retrieval_key[0] == source.upper()
                        ]
                        if partial and matching_retrievals:
                            retrieval_key, (next_retry_at, retry_count) = matching_retrievals[0]
                            now = utc_now_ms()
                            if retry_count < max_retrieval_attempts and now >= next_retry_at:
                                pending_retrievals[retrieval_key] = (
                                    now + retrieval_retry_delay_ms,
                                    retry_count,
                                )
                                database.audit(
                                    "inbox.retrieval_partial",
                                    {
                                        "custodian": retrieval_key[0],
                                        "js8call_message_id": retrieval_key[1],
                                        "attempt": retry_count,
                                        "message_id": legacy_id,
                                        "detail": "partial retrieval retained; scheduler will request the complete message again",
                                    },
                                )
                        elif not partial:
                            for retrieval_key in tuple(pending_retrievals):
                                if retrieval_key[0] == source.upper():
                                    database.audit(
                                        "inbox.retrieval_completed",
                                        {
                                            "custodian": retrieval_key[0],
                                            "js8call_message_id": retrieval_key[1],
                                            "attempt": pending_retrievals[retrieval_key][1],
                                            "message_id": legacy_id,
                                        },
                                    )
                                    completed_retrievals.add(retrieval_key)
                                    pending_retrievals.pop(retrieval_key, None)
                    query_response = parse_query_call_response(
                        frame.wire_text if frame is not None else ""
                    )
                    if (
                        query_response is not None
                        and isinstance(source, str)
                        and local_call
                        # The structured TO field carries our callsign, while
                        # the compact YES reply normally omits it from TEXT.
                        # A parsed recipient is therefore optional here.
                        and query_response.recipient in {None, local_call}
                        and command == "YES"
                    ):
                        now = utc_now_ms()
                        pending_call_queries[:] = [
                            query
                            for query in pending_call_queries
                            if now - query.submitted_at_ms <= LATE_QUERY_CONTEXT_MS
                        ]
                        matched_query = correlate_query_call_response(
                            pending_call_queries,
                            source,
                            now_ms=now,
                            band=str(status.get("band", "")),
                            max_age_ms=QUERY_RESPONSE_MAX_MS,
                        )
                        late_response = False
                        if matched_query is None:
                            # A delayed compact YES is still useful when it
                            # can be mapped unambiguously to one recent query.
                            # Keep this bounded to avoid attributing stale
                            # AllCall traffic to a newer message.
                            matched_query = correlate_query_call_response(
                                pending_call_queries,
                                source,
                                now_ms=now,
                                band=str(status.get("band", "")),
                                max_age_ms=LATE_QUERY_CONTEXT_MS,
                                allow_late=True,
                            )
                            late_response = matched_query is not None
                        if matched_query is not None:
                            queried_destination = matched_query.destination
                            # RX.ACTIVITY and RX.DIRECTED may expose partial
                            # and completed forms of the same compact answer.
                            # Since YES does not echo the query destination,
                            # suppress another answer from this source briefly
                            # rather than risk assigning a duplicate to a
                            # different outstanding @ALLCALL query.
                            answer_key = f"{source.upper()}:{matched_query.destination if matched_query else ''}"
                            for answer_key_old, answered_at in list(recent_query_answers.items()):
                                if now - answered_at >= query_context_window_ms:
                                    recent_query_answers.pop(answer_key_old, None)
                            if now - recent_query_answers.get(answer_key, 0) < 30_000:
                                matched_query = None
                            else:
                                recent_query_answers[answer_key] = now
                        if matched_query is not None:
                            snr = query_response.snr
                            age_minutes = query_response.age_minutes
                            observed_at = int(now - ((age_minutes or 0) * 60_000))
                            remote_params: dict[str, Any] = {
                                "FROM": source.upper(),
                                "TO": queried_destination,
                                "EVIDENCE": "remote_query_call_yes",
                            }
                            if snr is not None:
                                remote_params["SNR"] = snr
                            if age_minutes is not None:
                                remote_params["AGE_MIN"] = age_minutes
                            remote_link = NormalizedEvent(
                                "QUERY.CALL.RESPONSE",
                                event.value,
                                remote_params,
                                observed_at,
                            )
                            database.record_observation(
                                remote_link,
                                band=str(status.get("band", "")),
                                dial_frequency=status.get("dial_frequency"),
                            )
                            database.record_link_projection(
                                remote_link,
                                band=str(status.get("band", "")),
                                dial_frequency=status.get("dial_frequency"),
                            )
                            if local_call and local_call != source.upper():
                                local_snr = event.params.get("SNR")
                                reachability_params: dict[str, Any] = {
                                    "FROM": local_call,
                                    "TO": source.upper(),
                                    "EVIDENCE": "query_answered",
                                }
                                if isinstance(local_snr, (int, float)):
                                    reachability_params["SNR"] = local_snr
                                reachability_link = NormalizedEvent(
                                    "QUERY.CALL.REACHABILITY",
                                    event.value,
                                    reachability_params,
                                    now,
                                )
                                database.record_observation(
                                    reachability_link,
                                    band=str(status.get("band", "")),
                                    dial_frequency=status.get("dial_frequency"),
                                )
                                database.record_link_projection(
                                    reachability_link,
                                    band=str(status.get("band", "")),
                                    dial_frequency=status.get("dial_frequency"),
                                )
                            query_scheduler.record(
                                matched_query.scheduler_key,
                                int(asyncio.get_running_loop().time() * 1000),
                                success=True,
                            )
                            if matched_query.responder != "@ALLCALL":
                                pending_call_queries.remove(matched_query)
                            for message in database.list_messages(MessageState.WAITING_ROUTE):
                                if (
                                    str(message["destination"]).upper()
                                    == queried_destination.upper()
                                ):
                                    message_id = str(message["id"])
                                    if message_id not in route_evidence_settle_until_ms:
                                        settle_ms = route_evidence_settling_window_ms(
                                            matched_query.response_window_ms,
                                            status.get("speed", 0),
                                        )
                                        route_evidence_settle_until_ms[message_id] = now + settle_ms
                                        database.record_attempt(
                                            message_id,
                                            "route_evidence_settling",
                                            queried_destination,
                                            "waiting",
                                            f"collecting competing query replies for up to {settle_ms // 1000}s",
                                        )
                                    evidence_detail = (
                                        f"confirmed reachability to {queried_destination}"
                                        if snr is None
                                        else f"heard {queried_destination} at {snr} dB"
                                    )
                                    if age_minutes is not None:
                                        evidence_detail += f", {age_minutes} minute(s) ago"
                                    if late_response:
                                        evidence_detail += "; delayed query response"
                                    database.record_attempt(
                                        message_id,
                                        "route_evidence",
                                        source.upper(),
                                        "received",
                                        evidence_detail,
                                    )
                                    database.wake_message_for_route(message_id)
                    # A direct SNR response is the answer to the inexpensive
                    # reachability probe. Do not wait for the full defer
                    # interval before using it, but still let the single RF
                    # arbiter decide when the next payload may go out.
                    if (
                        frame is not None
                        and frame.command in {"SNR", "YES"}
                        and source
                        and frame.destination == local_call
                    ):
                        for message in database.list_messages(MessageState.WAITING_ROUTE):
                            message_id = str(message["id"])
                            route_probe_targets = {
                                str(attempt["target"]).upper()
                                for attempt in database.list_attempts(message_id)
                                if attempt["action"] == "route_probe"
                                and attempt["status"] in {"started", "submitted"}
                            }
                            if (
                                str(message["destination"]).upper() == source.upper()
                                or source.upper() in route_probe_targets
                            ):
                                database.record_attempt(
                                    message_id,
                                    "route_evidence",
                                    source.upper(),
                                    "received",
                                    "direct reachability response; stale route probe satisfied",
                                )
                                database.wake_message_for_route(message_id)
                    legacy_ack = parse_legacy_ack(frame) if frame is not None else None
                    if isinstance(source, str) and legacy_ack is not None:
                        ack_responder, ack_path = legacy_ack
                        matched = _recent_outbound_transaction(
                            database, ack_responder, utc_now_ms()
                        )
                        if matched is not None:
                            message, transaction = matched
                            message_id = str(message["id"])
                            database.acknowledge_transmission(int(transaction["id"]))
                            enhanced_message = bool(
                                database.list_message_parts(
                                    message_id,
                                    direction="outgoing",
                                    peer=str(message["destination"]),
                                )
                            )
                            operation = str(transaction["operation"])
                            if operation == "store":
                                database.upsert_custody(
                                    message_id,
                                    ack_responder,
                                    "accepted",
                                    "standard JS8Call store ACK",
                                )
                                database.record_attempt(
                                    message_id,
                                    "custody_ack",
                                    ack_responder,
                                    "received",
                                    "stored at custodian; recipient retrieval and delivery remain unproven",
                                )
                                if message["state"] not in {
                                    MessageState.STORED,
                                    MessageState.DELIVERED,
                                    MessageState.FAILED,
                                    MessageState.EXPIRED,
                                    MessageState.CANCELLED,
                                }:
                                    database.transition_message(message_id, MessageState.STORED)
                            else:
                                destination = str(message["destination"]).upper()
                                detail = (
                                    "standard JS8Call ACK; complete MSG accepted by destination inbox"
                                    if ack_responder == destination
                                    else "standard JS8Call final ACK returned through relay path"
                                )
                                if len(ack_path) > 1:
                                    detail += f" via {'→'.join(ack_path)}"
                                database.record_attempt(
                                    message_id, "standard_ack", ack_responder, "received", detail
                                )
                            if (
                                operation != "store"
                                and ack_responder == destination
                                and message["state"]
                                not in {
                                    MessageState.STORED,
                                    MessageState.DELIVERED,
                                    MessageState.FAILED,
                                    MessageState.EXPIRED,
                                    MessageState.CANCELLED,
                                }
                                and not enhanced_message
                            ):
                                database.transition_message(message_id, MessageState.DELIVERED)
                                origin = str(status.get("callsign", "")).upper()
                                if origin:
                                    try:
                                        speed = int(
                                            event.params.get("SPEED", status.get("speed", 0))
                                        )
                                    except (TypeError, ValueError):
                                        speed = 0
                                    ack_snr = event.params.get("SNR")
                                    database.record_link_outcome(
                                        origin,
                                        ack_responder,
                                        speed if speed in SPEED_AIRTIME_MS else 0,
                                        float(ack_snr)
                                        if isinstance(ack_snr, (int, float))
                                        else None,
                                        True,
                                        str(status.get("band", "")),
                                    )
                    if ack and isinstance(source, str):
                        kind, message_id, _bitmap = ack
                        known_capabilities = database.peer_capabilities(source)
                        inferred_features = set(known_capabilities[1] if known_capabilities else ())
                        inferred_features.update(("E2E",) if kind == "delivered" else ("MP", "PA"))
                        database.upsert_peer_capabilities(
                            source,
                            known_capabilities[0] if known_capabilities else 1,
                            tuple(sorted(inferred_features)),
                            utc_now_ms() + CAPABILITY_TTL_MS,
                        )
                        receipt_message = database.get_message(message_id)
                        if receipt_message is not None:
                            if kind == "delivered":
                                metadata = parse_delivery_ack(
                                    frame.payload if frame is not None else ""
                                )
                                receipt_path = metadata[2] if metadata is not None else ()
                                destination_matches = (
                                    source.upper() == str(receipt_message["destination"]).upper()
                                )
                                destination_name = str(receipt_message["destination"]).upper()
                                custody_rows = [
                                    item
                                    for item in database.list_custody(message_id)
                                    if item["status"]
                                    in {"accepted", "retrieval_pending", "forwarded"}
                                ]
                                receipt_nodes = {item.upper() for item in receipt_path}
                                forwarding_custodians = [
                                    item
                                    for item in custody_rows
                                    if item["custodian"].upper() in receipt_nodes
                                    and item["custodian"].upper() != destination_name
                                ]
                                # A final destination receipt may arrive from
                                # the destination itself, rather than from the
                                # custodian that forwarded it.  Correlate every
                                # proven custodian named in the receipt path.
                                forwarded_matches = bool(
                                    forwarding_custodians and destination_name in receipt_nodes
                                )
                                if destination_matches or forwarded_matches:
                                    detail = "end-to-end receipt"
                                    if metadata is not None:
                                        _, delivered_at_ms, path = metadata
                                        detail = (
                                            f"delivered_at={delivered_at_ms}; path={'→'.join(path)}"
                                        )
                                    database.record_attempt(
                                        message_id, "delivery_ack", source, "received", detail
                                    )
                                    if forwarded_matches:
                                        for custody_row in forwarding_custodians:
                                            custodian = str(custody_row["custodian"])
                                            database.upsert_custody(
                                                message_id,
                                                custodian,
                                                "forwarded",
                                                f"final receipt path includes {destination_name}",
                                            )
                                            database.record_attempt(
                                                message_id,
                                                "custodian_forwarded",
                                                custodian,
                                                "confirmed",
                                                detail,
                                            )
                                    if receipt_message["state"] not in {
                                        MessageState.DELIVERED,
                                        MessageState.FAILED,
                                        MessageState.EXPIRED,
                                        MessageState.CANCELLED,
                                    }:
                                        database.transition_message(
                                            message_id, MessageState.DELIVERED
                                        )
                            else:
                                part_ack = parse_part_ack(
                                    frame.payload if frame is not None else ""
                                )
                                if part_ack is not None:
                                    reconcile_part_receipt(
                                        database, receipt_message, part_ack, source
                                    )
                    parsed_part = (
                        parse_human_data_part(frame.payload) if frame is not None else None
                    )
                    if (
                        parsed_part is not None
                        and isinstance(source, str)
                        and source.upper() != status["callsign"]
                    ):
                        part, envelope_origin, envelope_destination = parsed_part
                        envelope_subject, user_payload = extract_envelope_subject(part.payload)
                        if envelope_subject:
                            part = MessagePart(
                                part.message_id,
                                part.number,
                                part.total,
                                user_payload,
                                part.origin,
                                part.destination,
                            )
                        # A valid JS8Mail data part is passive proof that this
                        # peer understands at least multipart framing. Do not
                        # infer E2E/PA from data alone; explicit CAP/receipts
                        # remain authoritative for those features.
                        known_capabilities = database.peer_capabilities(source)
                        inferred_features = set(known_capabilities[1] if known_capabilities else ())
                        inferred_features.add("MP")
                        database.upsert_peer_capabilities(
                            source,
                            known_capabilities[0] if known_capabilities else 1,
                            tuple(sorted(inferred_features)),
                            utc_now_ms() + CAPABILITY_TTL_MS,
                        )
                        if envelope_destination and envelope_destination.upper() != local_call:
                            # The surrounding JS8Call address and the
                            # explicit final destination disagree; do not
                            # turn a misaddressed frame into an inbox item.
                            parsed_part = None
                        else:
                            try:
                                logical_sender = (envelope_origin or source).upper()
                                reassembly_key = (logical_sender, part.message_id)
                                accumulator = reassembly.setdefault(
                                    reassembly_key,
                                    MultipartAccumulator(part.message_id, part.total),
                                )
                                if not accumulator.receipt().received:
                                    for stored_part in database.list_message_parts(
                                        part.message_id, direction="incoming", peer=logical_sender
                                    ):
                                        accumulator.add(
                                            MessagePart(
                                                part.message_id,
                                                int(stored_part["part_number"]),
                                                int(stored_part["total_parts"]),
                                                str(stored_part["payload"]),
                                            )
                                        )
                                accumulator.add(part)
                                database.upsert_message_part(
                                    part.message_id,
                                    part.number,
                                    part.total,
                                    part.payload,
                                    direction="incoming",
                                    peer=logical_sender,
                                )
                                receipt = accumulator.receipt()
                                route = tuple(
                                    item.strip().upper()
                                    for item in str(event.params.get("PATH", "")).split(">")
                                    if item.strip()
                                )
                                local_call = str(status.get("callsign", "")).upper()
                                reverse_route = tuple(reversed(route))
                                if local_call not in reverse_route:
                                    reverse_route = ()

                                def reply_text(body: str) -> str:
                                    if len(reverse_route) >= 3 and reverse_route[0] == local_call:
                                        return format_relay_text(reverse_route, body)
                                    return f"{logical_sender} {body}"

                                database.upsert_inbox_message(
                                    logical_sender,
                                    part.message_id,
                                    accumulator.partial_preview(),
                                    part.total,
                                    receipt.received,
                                    receipt.complete,
                                    route or (source,),
                                    envelope_destination
                                    if envelope_destination and envelope_destination.startswith("@")
                                    else "",
                                    protocol="js8m",
                                    delivery="forwarded" if len(route) >= 3 else "direct",
                                    subject=envelope_subject,
                                )
                                # The final DELIVERED receipt confirms the
                                # whole body; a separate final PA would add
                                # airtime without advancing the sender.
                                if not receipt.complete and accumulator.should_ack(utc_now_ms()):
                                    queue_protocol_reply(
                                        reply_text(format_part_ack(receipt)),
                                        logical_sender,
                                        part.message_id,
                                        "PART_ACK:" + receipt.as_bitmap(),
                                    )
                                if receipt.complete:
                                    queue_protocol_reply(
                                        reply_text(
                                            format_delivery_ack(
                                                part.message_id,
                                                utc_now_ms(),
                                                delivery_path_for_receipt(
                                                    logical_sender, local_call, route
                                                ),
                                            )
                                        ),
                                        logical_sender,
                                        part.message_id,
                                        "DELIVERED",
                                    )
                            except (ValueError, RuntimeError, ConnectionError):
                                database.audit("message.ack_failed", {"source": source})

                reader_task = asyncio.create_task(client.read_events(handle))
                try:
                    identity = await client.request_read_only("STATION.GET_CALLSIGN")
                    status["callsign"] = identity.value.strip().upper()
                    try:
                        frequency = await client.request_read_only("RIG.GET_FREQ")
                        apply_radio_context(dict(frequency.params))
                        if not status.get("dial_frequency"):
                            value = frequency.value.strip()
                            if value.isdigit():
                                status["dial_frequency"] = int(value)
                                status["band"] = band_from_frequency_hz(int(value))
                    except (ConnectionError, OSError, RuntimeError):
                        pass
                    try:
                        speed = await client.request_read_only("MODE.GET_SPEED")
                        reported_speed = speed.params.get("SPEED", speed.value.strip())
                        status["speed"] = reported_speed if reported_speed != "" else "unknown"
                        database.audit("js8call.speed_detected", {"speed": status["speed"]})
                    except (ConnectionError, OSError, RuntimeError):
                        status["speed"] = "unavailable"
                    await reader_task
                finally:
                    if tx_settle_task is not None:
                        tx_settle_task.cancel()
                        await asyncio.gather(tx_settle_task, return_exceptions=True)
                        tx_settle_task = None
                    tx_train.reset()
                    status["tx_train_pending"] = False
                    status["tx_reserved_ms"] = 0
                    status["pending_capability_message_id"] = None
                    status["pending_capability_peer"] = None
                    controller.active_transaction_id = None
                    if not reader_task.done():
                        reader_task.cancel()
                        await asyncio.gather(reader_task, return_exceptions=True)
            except (ConnectionError, OSError, asyncio.IncompleteReadError) as exc:
                status["connected"] = False
                database.audit("js8call.connection_error", {"error": type(exc).__name__})
            finally:
                status["connected"] = False
                await client.close()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
    finally:
        discovery_task.cancel()
        await asyncio.gather(discovery_task, return_exceptions=True)
        server.shutdown()
        server.server_close()
        database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=2442, type=int)
    parser.add_argument("--database", default="js8mail.sqlite3")
    parser.add_argument("--ui-host", default="127.0.0.1")
    parser.add_argument("--ui-port", default=8765, type=int)
    parser.add_argument(
        "--tx-mode",
        choices=("observe", "automatic"),
        default="automatic",
        help="RF handoff mode (default: automatic; use observe for receive-only)",
    )
    parser.add_argument(
        "--auto-speed",
        action="store_true",
        help="Allow the adapter to request evidence-backed JS8Call speed changes",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Open the local web UI after startup (used by the desktop bundle)",
    )
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
