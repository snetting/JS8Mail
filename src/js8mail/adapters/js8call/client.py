"""Async, bounded JS8Call TCP client with explicit RF safety gates."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from js8mail.adapters.js8call.protocol import (
    ApiMessage,
    ApiProtocolError,
    decode_line,
    encode_control_request,
    encode_read_only_request,
    encode_speed_request,
    encode_transmit_request,
)
from js8mail.domain import NormalizedEvent, utc_now_ms

EventHandler = Callable[[NormalizedEvent], Awaitable[None]]


class Js8CallClient:
    """One connection to JS8Call's JSON-line TCP API.

    Read-only requests, bounded automatic text submission, optional speed
    control, and the explicit operator halt are kept as separate adapter
    operations. Higher layers decide when a message is eligible for RF.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 2442) -> None:
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._handler: EventHandler | None = None
        self._pending: dict[str, tuple[asyncio.Future[ApiMessage], str]] = {}
        self._event_tasks: set[asyncio.Task[None]] = set()
        self._request_counter = 0

    def _request_id(self) -> str:
        self._request_counter += 1
        return f"{utc_now_ms()}-{self._request_counter}"

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)

    async def close(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        for future, _ in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("JS8Call connection closed"))
        self._pending.clear()
        tasks, self._event_tasks = self._event_tasks, set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def read_events(self, handler: EventHandler) -> None:
        if self._reader is None:
            raise RuntimeError("Client is not connected")
        self._handler = handler
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    return
                try:
                    message = decode_line(line.rstrip(b"\r\n"))
                except ApiProtocolError:
                    # A malformed API event is isolated to this frame. The
                    # caller can count/report it without taking down the daemon.
                    continue
                request_id = message.request_id
                pending_key = str(request_id) if request_id is not None else ""
                pending = self._pending.get(pending_key)
                if pending is None:
                    # Several JS8Call builds replace the client-supplied
                    # request ID with an internal numeric ID. Requests are
                    # issued serially by this adapter, so a response-type
                    # match is safe while still refusing to consume an
                    # unrelated asynchronous RX/TX event.
                    matches = [
                        (key, item)
                        for key, item in self._pending.items()
                        if item[1] == message.type or message.type == "API.ERROR"
                    ]
                    if len(matches) == 1:
                        pending_key, pending = matches[0]
                if pending is not None and not pending[0].done():
                    if message.type == "API.ERROR":
                        pending[0].set_exception(RuntimeError(message.value or "JS8Call API error"))
                    else:
                        pending[0].set_result(message)
                    continue
                event = NormalizedEvent(
                    event_type=message.type,
                    value=message.value,
                    params=message.params,
                    received_at_ms=utc_now_ms(),
                )

                async def dispatch(current_event: NormalizedEvent = event) -> None:
                    await handler(current_event)

                task: asyncio.Task[None] = asyncio.create_task(dispatch())
                self._event_tasks.add(task)
                task.add_done_callback(self._event_tasks.discard)
        finally:
            if self._event_tasks:
                await asyncio.gather(*tuple(self._event_tasks), return_exceptions=True)
            self._handler = None

    async def send_message(self, text: str) -> None:
        """Queue one validated human-readable message in JS8Call."""
        if self._writer is None or self._writer.is_closing():
            raise ConnectionError("JS8Call is not connected")
        current = await self.request_read_only("TX.GET_TEXT")
        if current.value.strip():
            raise RuntimeError("JS8Call transmit text is occupied by the operator")
        # Newer builds expose authoritative PTT and queue state. Older builds
        # may return an API error; in that case the existing TX.TEXT guard is
        # the strongest compatible check and the caller still records the
        # capability as unavailable.
        try:
            ptt = await self.request_read_only("RIG.GET_PTT")
        except (ConnectionError, TimeoutError, RuntimeError):
            ptt = None
        if ptt is not None and ptt.type == "RIG.PTT_STATUS":
            raw_ptt = str(ptt.params.get("PTT", ptt.value)).strip().lower()
            if raw_ptt in {"true", "on", "1", "tx"}:
                raise RuntimeError("JS8Call is already transmitting")
            if raw_ptt not in {"false", "off", "0", "rx", "idle", ""}:
                raise RuntimeError("JS8Call PTT state is unavailable")
        try:
            queue = await self.request_read_only("TX.GET_QUEUE_DEPTH")
        except (ConnectionError, TimeoutError, RuntimeError):
            queue = None
        if (
            queue is not None
            and queue.type == "TX.QUEUE_DEPTH"
            and int(queue.params.get("DEPTH", 0)) > 0
        ):
            raise RuntimeError("JS8Call transmit queue is occupied")
        request_id = self._request_id()
        # JS8Call's automatic API path is TX.SEND_MESSAGE with the text in
        # value. Sending an empty value only populates the UI text box.
        self._writer.write(encode_transmit_request("TX.SEND_MESSAGE", text, request_id=request_id))
        await self._writer.drain()

    async def request_read_only(self, request_type: str) -> ApiMessage:
        if self._writer is None or self._writer.is_closing():
            raise ConnectionError("JS8Call is not connected")
        request_id = self._request_id()
        future: asyncio.Future[ApiMessage] = asyncio.get_running_loop().create_future()
        expected_type = {
            "STATION.GET_CALLSIGN": "STATION.CALLSIGN",
            "RIG.GET_FREQ": "RIG.FREQ",
            "RIG.GET_PTT": "RIG.PTT_STATUS",
            "TX.GET_TEXT": "TX.TEXT",
            "TX.GET_QUEUE_DEPTH": "TX.QUEUE_DEPTH",
            "MODE.GET_SPEED": "MODE.SPEED",
        }.get(request_type, request_type)
        self._pending[request_id] = (future, expected_type)
        try:
            self._writer.write(encode_read_only_request(request_type, request_id=request_id))
            await self._writer.drain()
            return await asyncio.wait_for(future, timeout=5)
        finally:
            self._pending.pop(request_id, None)

    async def set_speed(self, speed: int) -> None:
        """Request a JS8Call speed change; callers must apply policy first."""
        if self._writer is None or self._writer.is_closing():
            raise ConnectionError("JS8Call is not connected")
        request_id = self._request_id()
        self._writer.write(encode_speed_request(speed, request_id=request_id))
        await self._writer.drain()

    async def halt(self) -> None:
        """Ask JS8Call to halt TX when the installed build supports it."""
        if self._writer is None or self._writer.is_closing():
            raise ConnectionError("JS8Call is not connected")
        self._writer.write(encode_control_request("RIG.TX_HALT", request_id=self._request_id()))
        await self._writer.drain()
