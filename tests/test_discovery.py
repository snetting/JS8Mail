from js8mail.discovery import (
    PendingCallQuery,
    QueryCallResponse,
    QueryScheduler,
    call_query,
    correlate_query_call_response,
    custodian_messages_query,
    hearing_query,
    messages_query,
    parse_messages_available,
    parse_messages_available_context,
    parse_query_call_response,
    retrieve_message_query,
    snr_query,
)


def test_standard_query_forms_are_bounded_and_normalized() -> None:
    assert hearing_query("n0call") == "N0CALL HEARING?"
    assert call_query("g0xyz") == "@ALLCALL QUERY CALL G0XYZ"
    assert messages_query() == "@ALLCALL QUERY MSGS"
    assert snr_query("g0abc") == "G0ABC SNR?"
    assert custodian_messages_query("g0abc") == "G0ABC QUERY MSGS"
    assert retrieve_message_query("g0abc", 42) == "G0ABC QUERY MSG 42"
    assert parse_messages_available("YES MSG ID 42") == 42
    assert parse_messages_available("NO") is None
    assert parse_query_call_response("OH3SPN YES") == QueryCallResponse("OH3SPN")
    assert parse_query_call_response("OH3SPN YES …… ♢") == QueryCallResponse("OH3SPN")
    assert parse_query_call_response("OH3SPN YES -08 (1M)") == QueryCallResponse("OH3SPN", -8, 1)
    assert parse_query_call_response("OH3SPN YES -25 (33M…… ♢") == QueryCallResponse(
        "OH3SPN", -25, 33
    )
    assert parse_query_call_response("OH3SPN YES -06 (45S ♢") == QueryCallResponse(
        "OH3SPN", -6, 0.75
    )
    assert parse_query_call_response("OH3SPN NO") is None
    assert parse_messages_available("OH3SPN YES MSG ID 42 ♢") == 42


def test_stored_message_announcement_preserves_custodian_and_recipient() -> None:
    assert parse_messages_available_context("MM0ZFG: OH3SPN YES MSG ID 431 ♢") == (
        "MM0ZFG",
        "OH3SPN",
        431,
    )
    assert parse_messages_available_context("YES MSG ID 431") == (None, None, 431)


def test_compact_yes_without_repeated_recipient_is_valid() -> None:
    assert parse_query_call_response("YES") == QueryCallResponse(None)


def test_query_call_response_uses_unambiguous_outstanding_context() -> None:
    pending = [PendingCallQuery(1_000, "SP2ST", "MM0ZFG", "candidate-query:MM0ZFG:SP2ST", "20m")]
    assert correlate_query_call_response(pending, "MM0ZFG", now_ms=2_000, band="20m") == pending[0]

    ambiguous = [
        PendingCallQuery(1_000, "SP2ST", "@ALLCALL", "call-query:SP2ST", "20m"),
        PendingCallQuery(1_500, "G0ABC", "@ALLCALL", "call-query:G0ABC", "20m"),
    ]
    assert correlate_query_call_response(ambiguous, "MM0ZFG", now_ms=2_000, band="20m") is None
    assert correlate_query_call_response(pending, "MM0ZFG", now_ms=200_000, band="20m") is None
    assert (
        correlate_query_call_response(
            pending, "MM0ZFG", now_ms=200_000, band="20m", max_age_ms=300_000, allow_late=True
        )
        == pending[0]
    )


def test_empty_queries_back_off_exponentially_and_success_resets() -> None:
    scheduler = QueryScheduler(base_delay_ms=100)
    assert scheduler.due("custodian", 0)
    scheduler.record("custodian", 0)
    assert not scheduler.due("custodian", 99)
    assert scheduler.due("custodian", 100)
    scheduler.record("custodian", 100)
    assert scheduler.state("custodian").next_at_ms == 300
    scheduler.record("custodian", 300, success=True)
    assert scheduler.state("custodian").attempts == 0
    assert scheduler.state("custodian").next_at_ms == 400
