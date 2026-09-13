"""Deterministic, radio-free scenarios for delivery and group safety tests."""

from __future__ import annotations

from dataclasses import dataclass, field

from js8mail.protocol import MessagePart, MultipartAccumulator, split_human_message


@dataclass(frozen=True, slots=True)
class SimTransmission:
    source: str
    target: str
    message_id: str
    part: int | None = None
    group: str | None = None


@dataclass(slots=True)
class SimNode:
    callsign: str
    inbox: dict[str, str] = field(default_factory=dict)
    groups: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class MultipartSimulationResult:
    complete: bool
    received: tuple[int, ...]
    missing: tuple[int, ...]
    resend_count: int
    transmissions: tuple[SimTransmission, ...]


class DeterministicRadioSimulation:
    """Small model of the protocol boundary, with no sockets or wall clock."""

    def __init__(self, *callsigns: str) -> None:
        self.nodes = {callsign.upper(): SimNode(callsign.upper()) for callsign in callsigns}
        self.transmissions: list[SimTransmission] = []
        self._group_acks: set[tuple[str, str]] = set()

    def multipart_delivery(
        self,
        message_id: str,
        body: str,
        path: tuple[str, ...],
        *,
        drop_once: frozenset[tuple[int, str]] = frozenset(),
    ) -> MultipartSimulationResult:
        if len(path) < 2 or any(call.upper() not in self.nodes for call in path):
            raise ValueError("simulation path must contain known nodes")
        parts = split_human_message(message_id, body, chunk_bytes=64)
        destination = path[-1].upper()
        accumulator = MultipartAccumulator(message_id, len(parts))
        dropped: set[tuple[int, str]] = set()

        def forward(part: MessagePart) -> None:
            for index in range(1, len(path)):
                source, target = path[index - 1].upper(), path[index].upper()
                key = (part.number, target)
                self.transmissions.append(SimTransmission(source, target, message_id, part.number))
                if key in drop_once and key not in dropped:
                    dropped.add(key)
                    return
            accumulator.add(part)

        for part in parts:
            forward(part)
        missing = accumulator.receipt().missing
        # The destination's selective ACK requests only missing parts. The
        # request is routed back over the known custody path, then the part is
        # forwarded again across every hop.
        for number in missing:
            forward(parts[number - 1])
        receipt = accumulator.receipt()
        if receipt.complete:
            self.nodes[destination].inbox[message_id] = accumulator.assembled() or ""
        return MultipartSimulationResult(
            receipt.complete,
            receipt.received,
            receipt.missing,
            len(missing),
            tuple(self.transmissions),
        )

    def group_broadcast(
        self,
        message_id: str,
        group: str,
        body: str,
        recipients: tuple[str, ...],
        *,
        designated_ack: str | None = None,
    ) -> tuple[SimTransmission, ...]:
        group = group.upper()
        if not group.startswith("@") or any(recipient.upper() not in self.nodes for recipient in recipients):
            raise ValueError("invalid group simulation")
        for recipient in recipients:
            node = self.nodes[recipient.upper()]
            if message_id in node.inbox:
                continue
            node.inbox[message_id] = body
            self.transmissions.append(SimTransmission("ORIGIN", recipient.upper(), message_id, group=group))
        ack_key = (group, message_id)
        if designated_ack is not None and designated_ack.upper() in self.nodes and ack_key not in self._group_acks:
            self.transmissions.append(SimTransmission(designated_ack.upper(), "ORIGIN", message_id, group=group))
            self._group_acks.add(ack_key)
        return tuple(self.transmissions)
