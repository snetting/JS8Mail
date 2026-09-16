from js8mail.bands import band_from_frequency_hz, context_from_params


def test_small_vfo_change_stays_in_same_band() -> None:
    assert band_from_frequency_hz(14_078_000) == "20m"
    assert band_from_frequency_hz(14_200_000) == "20m"


def test_unknown_frequency_is_not_routable_band_evidence() -> None:
    assert band_from_frequency_hz(12_345_678) == ""


def test_context_prefers_explicit_band_and_retains_dial() -> None:
    assert context_from_params({"BAND": "20M", "DIAL": 14_078_000}) == ("20m", 14_078_000)
