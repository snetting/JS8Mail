import asyncio

import pytest

from js8mail.adapters.js8call.client import Js8CallClient


@pytest.mark.asyncio
async def test_client_captures_events_and_ignores_malformed_frames() -> None:
    received: list[object] = []

    async def collect(event: object) -> None:
        received.append(event)

    async def server_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"not-json\n")
        writer.write(b'{"type":"RX.ACTIVITY","value":"N0CALL: HI","params":{"SNR":-12}}\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(server_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = Js8CallClient(port=port)
    try:
        await client.connect()
        await client.read_events(collect)
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    assert len(received) == 1
    assert received[0].event_type == "RX.ACTIVITY"  # type: ignore[union-attr]
