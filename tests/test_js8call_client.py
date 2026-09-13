import asyncio
import json

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


@pytest.mark.asyncio
async def test_client_checks_and_sends_message() -> None:
    received: list[dict[str, object]] = []

    async def server_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        check = json.loads(await reader.readline())
        writer.write(
            (
                json.dumps({"type": "TX.TEXT", "value": "", "params": check["params"]}) + "\n"
            ).encode()
        )
        await writer.drain()
        received.append(json.loads(await reader.readline()))
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(server_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = Js8CallClient(port=port)

    async def ignore_event(_: object) -> None:
        return

    try:
        await client.connect()
        reader_task = asyncio.create_task(client.read_events(ignore_event))
        await client.send_message("N0CALL test")
        await asyncio.sleep(0)
        await reader_task
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    assert [packet["type"] for packet in received] == ["TX.SEND_MESSAGE"]
    assert received[0]["value"] == "N0CALL test"
