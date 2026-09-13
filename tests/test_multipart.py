import pytest

from js8mail.protocol import (
    MessagePart,
    MultipartAccumulator,
    MultipartError,
    format_human_data_part,
    format_part_ack,
)


def test_reassembly_reports_missing_parts_and_assembles_in_order() -> None:
    accumulator = MultipartAccumulator("m1", 3)
    assert accumulator.add(MessagePart("m1", 2, 3, "B"))
    assert accumulator.add(MessagePart("m1", 1, 3, "A"))
    receipt = accumulator.receipt()
    assert receipt.missing == (3,)
    assert not receipt.complete
    assert accumulator.partial_preview() == "AB[MISSING PART 3/3]"
    assert "m1 3" in format_part_ack(receipt)
    assert accumulator.add(MessagePart("m1", 3, 3, "C"))
    assert accumulator.assembled() == "ABC"


def test_duplicate_part_is_suppressed() -> None:
    accumulator = MultipartAccumulator("m1", 2)
    part = MessagePart("m1", 1, 2, "A")
    assert accumulator.add(part)
    assert not accumulator.add(part)


def test_inconsistent_or_oversized_parts_are_rejected() -> None:
    accumulator = MultipartAccumulator("m1", 2)
    with pytest.raises(MultipartError):
        accumulator.add(MessagePart("m1", 1, 3, "A"))
    with pytest.raises(MultipartError):
        MessagePart("m1", 1, 2, "x" * 5000)


def test_ack_is_rate_limited_without_hiding_new_parts() -> None:
    accumulator = MultipartAccumulator("m1", 2)
    accumulator.add(MessagePart("m1", 1, 2, "A"))
    assert accumulator.should_ack(1000)
    assert not accumulator.should_ack(1001)
    assert accumulator.should_ack(31_001)
    accumulator.add(MessagePart("m1", 2, 2, "B"))
    assert accumulator.should_ack(31_002)


def test_body_part_remains_readable_while_metadata_is_tagged() -> None:
    encoded = format_human_data_part(MessagePart("m1", 1, 2, "EVACUATE NORTH NOW"))
    assert encoded.startswith("J8M1 D m1 1/2 ")
    assert "EVACUATE NORTH NOW" in encoded
