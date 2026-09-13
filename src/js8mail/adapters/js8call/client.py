"""Async, bounded, receive-first JS8Call TCP client."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from js8mail.adapters.js8call.protocol import (
    ApiMessage,
    ApiProtocolError,
    decode_line,
    encode_read_only_request,
    encode_transmit_request,
)
from js8mail.domain import NormalizedEvent, utc_now_ms

EventHandler = Callable[[NormalizedEvent], Awaitable[None]]


class Js8CallClient:
    """One connection to JS8Call's JSON-line TCP API.

    The initial client is intentionally receive-only. It can issue bounded
    read-only probes supplied by a future capability manager, but has no method
    that submits transmit text.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 2442) -> None:
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._handler: EventHandler | None = None
        self._pending: dict[str, asyncio.Future[ApiMessage]] = {}

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)

    async def close(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("JS8Call connection closed"))
        self._pending.clear()
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
                pending = self._pending.get(str(request_id)) if request_id is not None else None
                if pending is not None and not pending.done():
                    pending.set_result(message)
                    continue
                await handler(
                    NormalizedEvent(
                        event_type=message.type,
                        value=message.value,
                        params=message.params,
                        received_at_ms=utc_now_ms(),
                    )
                )
        finally:
            self._handler = None

    async def send_message(self, text: str) -> None:
        """Queue one operator-approved human-readable message in JS8Call."""
        if self._writer is None or self._writer.is_closing():
            raise ConnectionError("JS8Call is not connected")
        current = await self.request_read_only("TX.GET_TEXT")
        if current.value.strip():
            raise RuntimeError("JS8Call transmit text is occupied by the operator")
        request_id = str(utc_now_ms())
        self._writer.write(encode_transmit_request("TX.SET_TEXT", text, request_id=request_id))
        self._writer.write(encode_transmit_request("TX.SEND_MESSAGE", "", request_id=request_id))
        await self._writer.drain()

    async def request_read_only(self, request_type: str) -> ApiMessage:
        if self._writer is None or self._writer.is_closing():
            raise ConnectionError("JS8Call is not connected")
        request_id = str(utc_now_ms())
        future: asyncio.Future[ApiMessage] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            self._writer.write(encode_read_only_request(request_type, request_id=request_id))
            await self._writer.drain()
            return await asyncio.wait_for(future, timeout=5)
        finally:
            self._pending.pop(request_id, None)
