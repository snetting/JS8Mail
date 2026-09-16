from js8mail.confidence import ConfidenceLevel, DeliveryEvidence, describe


def test_legacy_hop_ack_never_becomes_end_to_end() -> None:
    evidence = DeliveryEvidence(submitted=True, frames_observed=2, hop_acknowledged=True)
    assert evidence.level(enhanced_peer=False) == ConfidenceLevel.HOP_ACKNOWLEDGED
    assert "end-to-end" in describe(evidence, enhanced_peer=False)


def test_enhanced_receipt_is_stronger_than_complete_parts() -> None:
    evidence = DeliveryEvidence(parts_complete=True, end_to_end_receipt=True)
    assert evidence.level(enhanced_peer=True) == ConfidenceLevel.END_TO_END
    assert evidence.level(enhanced_peer=False) == ConfidenceLevel.NONE
