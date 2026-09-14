# JS8Mail

Reliable offline-first radio mail above an unmodified JS8Call instance.

JS8Mail is intended for dependable comms when Internet access is
unavailable, including emergency and disaster scenarios. It keeps the mailbox,
observations, routing decisions, retries, and audit history locally, then uses
JS8Call for the actual RF modem and transmission. A message can be sent direct,
relayed through stations suggested by observed RF evidence, or stored with a
custodian for later collection.

Long messages are split into numbered JS8Mail parts for compatible peers. The
receiver displays a useful partial message, marks missing parts, and can request
only those parts again. Duplicate parts and receipts are safely ignored. A
standard JS8Call recipient still receives readable ordinary text; enhanced
JS8Mail framing is used only after the peer capability exchange proves support.

The project is early development and experimental. Delivery status is therefore
carefully qualified: submission to JS8Call, a hop acknowledgement, custodian
storage, and end-to-end JS8Mail delivery are different facts. Routing is
progressive: a short direct reachability probe comes first, then current RF
evidence, selective path discovery, alternative routes, bounded retries, and
time-aware backoff. Internet topology data may enhance local decisions in a
future service, but it is never required for RF operation.

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

Automatic submission hands text to JS8Call; the UI reports only delivery
evidence actually observed. Use a dummy load, suitable low-power settings, and
follow local licensing and band-plan rules. The original receive-only probe
remains available as `js8mail-probe`.

JS8Call setup: enable its documented local TCP/JSON API on loopback (normally
port 2442), ensure JS8Call has the correct audio input/output devices, and
enable the heartbeat, relay, inbox/store-and-forward, and decode facilities
you want to use. JS8Mail does not edit unknown JS8Call configuration files;
the UI should be treated as setup guidance and the operator remains in control
of RF transmission. Intermediate stations may not have these facilities, so
the routing logic degrades to the strongest standard JS8Call behavior available.

Linux is the current development platform. A packaged Windows build is planned;
Python applications can be distributed as compiled/frozen executables (for
example with PyInstaller), so users should not ultimately need to install or
manage a Python environment manually.

For the complete operator guide—including JS8Call setup, TCP/API configuration,
the end-to-end message lifecycle, path discovery, route scoring, custody,
multipart recovery, groups, airtime protection, and troubleshooting—see the
[JS8Mail user and operator guide](docs/USER_GUIDE.md).

Also see the versioned [JS8Mail protocol specification](docs/PROTOCOL_V1.md),
the [API capability matrix](docs/API_CAPABILITY_MATRIX.md), and
[`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) for protocol,
compatibility, and staged implementation boundaries.
