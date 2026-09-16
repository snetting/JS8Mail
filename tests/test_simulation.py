from js8mail.simulation import DeterministicRadioSimulation


def test_three_hop_multipart_recovers_one_missing_part() -> None:
    simulation = DeterministicRadioSimulation("ORIGIN", "RELAY1", "RELAY2", "DEST")
    result = simulation.multipart_delivery(
        "m1",
        "A long emergency message that is deliberately split across four JS8Mail parts. "
        "0123456789 ABCDEFGHIJ 0123456789 ABCDEFGHIJ 0123456789 ABCDEFGHIJ "
        "0123456789 ABCDEFGHIJ 0123456789 ABCDEFGHIJ",
        ("ORIGIN", "RELAY1", "RELAY2", "DEST"),
        drop_once=frozenset({(2, "RELAY2")}),
    )
    assert result.complete
    assert max(item.part or 0 for item in result.transmissions) <= 4
    assert result.missing == ()
    assert result.resend_count == 1
    assert simulation.nodes["DEST"].inbox["m1"].startswith("A long emergency")


def test_group_broadcast_deduplicates_and_limits_ack_to_designated_station() -> None:
    simulation = DeterministicRadioSimulation("ORIGIN", "A", "B", "COORD")
    first = simulation.group_broadcast("g1", "@EMCOMM", "Need assistance", ("A", "B"), designated_ack="COORD")
    second = simulation.group_broadcast("g1", "@EMCOMM", "Need assistance", ("A", "B"), designated_ack="COORD")
    assert len(first) == 3
    assert len(second) == 3
    assert sum(item.target == "ORIGIN" for item in second) == 1
    assert simulation.nodes["A"].inbox == {"g1": "Need assistance"}
