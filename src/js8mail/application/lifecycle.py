"""Durable message lifecycle rules."""

from __future__ import annotations

from enum import StrEnum


class MessageState(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    WAITING_ROUTE = "waiting_route"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_OPPORTUNITY = "waiting_opportunity"
    IN_PROGRESS = "in_progress"
    DELIVERED = "delivered"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset(
    {MessageState.DELIVERED, MessageState.FAILED, MessageState.EXPIRED, MessageState.CANCELLED}
)


def can_transition(current: MessageState, target: MessageState) -> bool:
    if current in TERMINAL_STATES:
        return False
    if current == target:
        return True
    allowed = {
        MessageState.DRAFT: {MessageState.QUEUED, MessageState.CANCELLED},
        MessageState.QUEUED: {
            MessageState.WAITING_ROUTE,
            MessageState.CANCELLED,
            MessageState.EXPIRED,
        },
        MessageState.WAITING_ROUTE: {
            MessageState.WAITING_APPROVAL,
            MessageState.WAITING_OPPORTUNITY,
            MessageState.IN_PROGRESS,
            MessageState.FAILED,
            MessageState.EXPIRED,
            MessageState.CANCELLED,
        },
        MessageState.WAITING_APPROVAL: {
            MessageState.WAITING_OPPORTUNITY,
            MessageState.IN_PROGRESS,
            MessageState.CANCELLED,
            MessageState.EXPIRED,
        },
        MessageState.WAITING_OPPORTUNITY: {
            MessageState.IN_PROGRESS,
            MessageState.FAILED,
            MessageState.EXPIRED,
            MessageState.CANCELLED,
        },
        MessageState.IN_PROGRESS: {
            MessageState.WAITING_ROUTE,
            MessageState.DELIVERED,
            MessageState.FAILED,
            MessageState.EXPIRED,
            MessageState.CANCELLED,
        },
    }
    return target in allowed.get(current, set())
