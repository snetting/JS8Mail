from js8mail.groups import default_group_description, extract_groups


def test_group_detection_normalizes_and_deduplicates() -> None:
    assert extract_groups("@emcomm report", "heard @DX/EU and @emcomm") == ("@DX/EU", "@EMCOMM")
    assert default_group_description("@WX") == "weather"
