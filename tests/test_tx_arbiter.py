from js8mail.application.tx_arbiter import (
    AutomationMode,
    RadioSnapshot,
    TransmitArbiter,
    TransmitIntent,
)


def ready_radio() -> RadioSnapshot:
    return RadioSnapshot(True, True, False, False, True)


def test_observe_mode_never_transmits() -> None:
    decision = TransmitArbiter().decide(TransmitIntent("a", "HELLO"), ready_radio())
    assert not decision.allowed


def test_approval_mode_requires_exact_operator_approval() -> None:
    arbiter = TransmitArbiter(AutomationMode.APPROVE)
    assert not arbiter.decide(TransmitIntent("a", "HELLO"), ready_radio()).allowed
    assert arbiter.decide(TransmitIntent("a", "HELLO", approved=True), ready_radio()).allowed


def test_unknown_manual_or_ptt_state_defers() -> None:
    arbiter = TransmitArbiter(AutomationMode.AUTOMATIC)
    radio = RadioSnapshot(True, None, False, False)
    assert not arbiter.decide(TransmitIntent("a", "HELLO"), radio).allowed


def test_dry_run_is_allowed_but_is_not_a_submission() -> None:
    decision = TransmitArbiter().decide(TransmitIntent("a", "HELLO", dry_run=True), ready_radio())
    assert decision.allowed
    assert "dry-run" in decision.reason
