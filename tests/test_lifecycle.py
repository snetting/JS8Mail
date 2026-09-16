from js8mail.application.lifecycle import MessageState, can_transition


def test_lifecycle_allows_planning_and_rejects_terminal_revival() -> None:
    assert can_transition(MessageState.QUEUED, MessageState.WAITING_ROUTE)
    assert can_transition(MessageState.IN_PROGRESS, MessageState.DELIVERED)
    assert not can_transition(MessageState.DELIVERED, MessageState.QUEUED)


def test_expiry_is_available_before_transmission() -> None:
    assert can_transition(MessageState.WAITING_OPPORTUNITY, MessageState.EXPIRED)
