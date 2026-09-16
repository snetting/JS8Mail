"""Receive-only JS8Call API probe.

This tool never sends a transmit command. It observes events and exits when
the connection closes or Ctrl-C is pressed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

from js8mail.adapters.js8call.client import Js8CallClient
from js8mail.domain import NormalizedEvent
from js8mail.storage import Database


async def run(host: str, port: int, database_path: str) -> None:
    database = Database(Path(database_path).expanduser())
    client = Js8CallClient(host, port)

    async def handle(event: NormalizedEvent) -> None:
        # The adapter guarantees NormalizedEvent, while keeping this tool's
        # output intentionally raw enough to compare against API documentation.
        database.record_observation(event)
        print(json.dumps(asdict(event), default=str, sort_keys=True), flush=True)

    try:
        await client.connect()
        print(f"connected to {host}:{port}; receive-only probe active", file=sys.stderr)
        await client.read_events(handle)
    finally:
        await client.close()
        database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=2442, type=int)
    parser.add_argument("--database", default="js8mail-probe.sqlite3")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.host, args.port, args.database))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
