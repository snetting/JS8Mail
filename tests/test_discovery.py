from js8mail.discovery import (
    QueryScheduler,
    call_query,
    custodian_messages_query,
    hearing_query,
    messages_query,
    parse_messages_available,
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
    assert parse_query_call_response("OH3SPN YES -08 (1M)") == (-8, 1)
    assert parse_query_call_response("OH3SPN YES -25 (33M)") == (-25, 33)
    assert parse_query_call_response("OH3SPN NO") is None


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
