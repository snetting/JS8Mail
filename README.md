# JS8Mail

Offline-first messaging above an unmodified JS8Call instance.

The current implementation is deliberately receive-first. It provides bounded
JS8Call JSON parsing, passive observation capture, durable SQLite message
queueing, guarded lifecycle transitions, dry-run transmit decisions, and
optional-topology request signing primitives. It does not submit RF
transmissions yet.

## Development

Python 3.12 or newer is required.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
pytest
```

The receive-only probe can be run against a locally configured JS8Call API:

```sh
python -m js8mail.tools.probe_js8call_api --host 127.0.0.1 --port 2442
```

The probe never constructs a transmit command. See
[`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) for the staged
implementation and safety boundaries.
