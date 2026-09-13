from js8mail.discovery import QueryScheduler, call_query, hearing_query, messages_query, snr_query


def test_standard_query_forms_are_bounded_and_normalized() -> None:
    assert hearing_query("n0call") == "N0CALL HEARING?"
    assert call_query("g0xyz") == "@ALLCALL QUERY CALL G0XYZ"
    assert messages_query() == "@ALLCALL QUERY MSGS"
    assert snr_query("g0abc") == "G0ABC SNR?"


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
