# JS8Mail

Offline-first messaging above an unmodified JS8Call instance.

The current implementation is deliberately conservative. It provides bounded
JS8Call JSON parsing, passive observation capture, durable SQLite message
queueing, guarded lifecycle transitions, a local mailbox UI, and explicit
operator-approved JS8Call transmission. It does not perform automatic routing
or claim end-to-end delivery yet.

## Development

Python 3.12 or newer is required.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
pytest
```

The local mailbox can be run against a locally configured JS8Call API:

```sh
./start.sh --host 127.0.0.1 --port 2442 --ui-port 8765 --tx-mode automatic
```

Open http://127.0.0.1:8765 in a browser. `automatic` submits queued messages
to JS8Call for its next transmit cycle. `observe` allows queueing but no RF
submission:

```sh
./start.sh --host 127.0.0.1 --port 2442 --ui-port 8765 --tx-mode observe
```

Automatic submission only hands text to JS8Call; it does not report delivery.
Use a dummy load and suitable low-power test setup. The original receive-only
probe remains available as `js8mail-probe`. See
[`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) for the staged
implementation and safety boundaries.
