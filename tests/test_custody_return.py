from js8mail.tools.app import reverse_custody_path


def test_reverse_custody_path_reverses_known_incoming_path() -> None:
    assert reverse_custody_path(
        "N1ABC", "OH3SPN", ("OH3SPN", "MM0ZFG", "N1ABC")
    ) == ("N1ABC", "MM0ZFG", "OH3SPN")


def test_reverse_custody_path_rejects_path_without_original_sender() -> None:
    assert reverse_custody_path(
        "N1ABC", "OH3SPN", ("MM0ZFG", "N1ABC")
    ) == ()
