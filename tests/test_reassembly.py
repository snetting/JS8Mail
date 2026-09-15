from js8mail.reassembly import ActivityAssembler, ActivityFragment, first_frame, last_frame


def fragment(value, bits, t, *, offset=None):
    return ActivityFragment(value, bits, t, t, "20m", 14078000, offset, 1)


def test_js8call_bits_use_masks_and_reassemble_first_middle_last():
    assert first_frame(5)
    assert last_frame(6)
    assembler = ActivityAssembler()
    assert assembler.feed(fragment("F4LPU: OH3SPN MSG ", 1, 1000), local_destination="OH3SPN")[0].confidence == "partial"
    assembler.feed(fragment(" HELLO ", 4, 2000), local_destination="OH3SPN")
    result = assembler.feed(fragment("WORLD", 2, 3000), local_destination="OH3SPN")[0]
    assert result.complete
    assert result.confidence == "reassembled"
    assert ActivityAssembler.clean_text(result.text) == "F4LPU: OH3SPN MSGHELLOWORLD"


def test_single_frame_and_unknown_extra_bits_are_supported():
    assembler = ActivityAssembler()
    result = assembler.feed(
        fragment("F4LPU: OH3SPN MSG SHORT", 3, 1000), local_destination="OH3SPN"
    )[0]
    assert result.complete
    assembler = ActivityAssembler()
    assembler.feed(fragment("F4LPU: OH3SPN MSG ", 9, 1000), local_destination="OH3SPN")
    result = assembler.feed(fragment("DONE", 6, 2000), local_destination="OH3SPN")[0]
    assert result.complete


def test_prefixed_unrelated_activity_does_not_clear_pending_message():
    assembler = ActivityAssembler()
    assembler.feed(fragment("F4LPU: OH3SPN MSG PART ONE ", 1, 1000), local_destination="OH3SPN")
    assert not assembler.feed(fragment("M0OUE: G0ABC MSG OTHER", 3, 1500), local_destination="OH3SPN")
    result = assembler.feed(fragment("PART TWO", 2, 2000), local_destination="OH3SPN")[0]
    assert result.complete
    assert "PART ONE" in result.text and "PART TWO" in result.text


def test_two_matching_streams_never_guess_a_continuation():
    assembler = ActivityAssembler()
    assembler.feed(fragment("F4LPU: OH3SPN MSG A", 1, 1000, offset=100), local_destination="OH3SPN")
    assembler.feed(fragment("M0OUE: OH3SPN MSG B", 1, 1100, offset=100), local_destination="OH3SPN")
    results = assembler.feed(fragment("CONT", 4, 1200, offset=100), local_destination="OH3SPN")
    assert len(results) == 2
    assert all(item.ambiguous and not item.complete for item in results)


def test_out_of_order_observation_is_not_reported_as_confident_completion():
    assembler = ActivityAssembler()
    assembler.feed(fragment("F4LPU: OH3SPN MSG A", 1, 2000), local_destination="OH3SPN")
    result = assembler.feed(fragment("B", 2, -1000), local_destination="OH3SPN")[0]
    assert result.complete is False
    assert result.ambiguous


def test_legacy_no_bits_uses_ellipsis_only_as_provisional_completion():
    assembler = ActivityAssembler()
    assembler.feed(fragment("F4LPU: OH3SPN MSG OLD ", None, 1000), local_destination="OH3SPN")
    result = assembler.feed(fragment("FORMAT……", None, 2000), local_destination="OH3SPN")[0]
    assert result.complete
    assert result.confidence == "legacy"
    assert ActivityAssembler.clean_text(result.text).endswith("OLDFORMAT")
