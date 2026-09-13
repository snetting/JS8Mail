# JS8Mail implementation plan

Status: planning baseline, 2026-09-13

## 1. Outcome and scope

Build JS8Mail as a Linux-first, offline-first mail client and asynchronous daemon above an unmodified JS8Call instance. JS8Call remains responsible for modulation, decoding, audio, rig control, PTT, and the existing on-air protocol. JS8Mail owns durable mail state, observations, routing decisions, safe scheduling, enhanced-peer semantics, auditability, and the operator interface.

The implementation should target the lowest verified API baseline first: stock JS8Call 2.3.1. Newer JS8Call-improved capabilities may improve safety and visibility but must be optional feature gates. No route, transmission, or state transition may depend on Internet access.

Phase 1 is successful only when a small vertical path works end to end:

1. connect to JS8Call;
2. passively capture and persist observations;
3. queue a message durably;
4. plan a conservative direct action;
5. obtain approval or run in dry-run mode;
6. arbitrate safely with JS8Call's compose/transmit state;
7. record exactly what was attempted and what was actually proven;
8. recover correctly after daemon restart.

Everything else should grow from that path.

## 2. Verified environment and source baseline

The planning inspection found:

| Item | Verified value | Consequence |
|---|---|---|
| Installed stock application | JS8Call 2.3.1 Fedora package | This is the compatibility floor for v1. |
| Locally built improved application | JS8Call-improved 2.4.0, commit `6d158d8a10de7a05026af67777d1dac8d1e60c14` | Useful for source inspection and later non-RF integration tests. |
| Current improved documentation | API documentation labelled 3.0.0; current downloads are newer than the local build | Treat documentation as version-specific, not proof that an installed command exists. |
| Current local JS8Call settings | TCP disabled; TCP requests disabled; UDP enabled on `127.0.0.1:2242`; configured TCP port `2442` | Do not edit the INI file. Provide guided setup and runtime probes after explicit operator action. |
| Running JS8Call process | None during inspection | No live API behavior has yet been verified. |
| Python available | 3.14.6 | Declare support as Python `>=3.12`; CI must test at least 3.12 and the current stable version. |
| Platform | Fedora Linux x86_64 | Linux packaging first; keep paths and process management portable. |

Primary references used for this baseline:

- [JS8Call upstream repository and 2.3.1 release](https://github.com/js8call/js8call)
- [JS8Call-improved 2.4.0 source](https://github.com/JS8Call-improved/JS8Call-improved/tree/release/2.4.0)
- [JS8Call-improved API documentation v3.0.0](https://js8call.com/JS8Call-improved/d7/d15/md_docs_2API.html)

Source inspection of 2.4.0 verifies the 2.3-era request handlers and asynchronous events in `mainwindow.cpp`. Runtime behavior, response correlation, malformed input behavior, socket framing, queue races, and each installed binary's precise capabilities still require a live, non-transmitting probe.

## 3. Requirement analysis and design decisions

### 3.1 Separate facts from conclusions

The system must store immutable evidence before deriving route or delivery conclusions. For example:

- `RIG.PTT on` is evidence that JS8Call keyed PTT, not proof that a complete message was transmitted.
- `TX.FRAME` is evidence that a frame was emitted, not proof that it was decoded.
- a JS8Call `ACK` from an intermediate station is a hop acknowledgement, not end-to-end delivery;
- inbox acceptance by a relay is custody/store evidence only when the exact JS8Call exchange or enhanced custody receipt proves it;
- only a valid enhanced end-to-end delivery receipt from the destination can set `delivered` automatically;
- a read receipt is separate, optional, and never inferred.

This distinction drives the database, state machines, API, UI wording, and tests.

### 3.2 Use orthogonal state machines

Do not implement one large message status enum. Use four related state machines:

1. **Message delivery lifecycle** — the user's durable intent and terminal result.
2. **Attempt lifecycle** — one direct, relay, store, retrieval, or protocol-control attempt.
3. **Custody lifecycle** — which station is proven to hold responsibility for an enhanced message.
4. **Receipt lifecycle** — delivery/read/custody receipt production, transmission, and consumption.

This prevents a late ACK, duplicate frame, or restarted scheduler from promoting a message incorrectly.

### 3.3 Default to a local web UI

Use a local daemon HTTP/WebSocket API and a server-rendered local web UI. This gives a simple Linux MVP and a viable Windows/macOS path without coupling the UI to the radio socket or database. Keep the UI deliberately plain until safety, state recovery, and accurate status semantics pass acceptance tests.

Recommended initial stack:

- Python 3.12+;
- `asyncio` and typed domain code;
- FastAPI/Starlette for the loopback-only daemon API and event stream;
- server-rendered HTML with a small amount of vanilla JavaScript; avoid a Node build in the MVP;
- SQLite in WAL mode, with explicit migrations and one application-level writer;
- SQL written through a narrow repository layer; use bound parameters only;
- Pydantic at trust boundaries, dataclasses or immutable value objects in the domain;
- pytest, pytest-asyncio, Hypothesis where stateful/property testing helps, Ruff, and mypy/pyright;
- a small internal migration runner rather than adopting a heavy ORM.

Bind the UI/API to loopback by default. If remote UI access is ever added, it is a separate security feature and not part of v1.

### 3.4 Keep transport boundaries narrow

Define a modem adapter protocol around domain facts and intents, not around every raw JSON field:

- connection/capability snapshot;
- normalized receive event stream;
- read-only station/rig/transmit state queries;
- validated transmit intent submission;
- halt/cancel where supported;
- raw-event capture for diagnostics and replay.

The JS8Call adapter owns all JSON names, version differences, framing, and quirks. Core routing and lifecycle code must not import JS8Call socket classes or inspect raw JS8Call dictionaries.

### 3.5 Make scheduling durable and event driven

SQLite is the source of truth. The in-memory scheduler is a projection rebuilt after restart. Every planned action has:

- a stable action ID;
- earliest and latest execution times;
- reason and evidence snapshot;
- policy/budget snapshot;
- required capability set;
- approval state;
- lease owner and lease expiry;
- idempotency key;
- outcome event.

Claim work using a short database lease inside a transaction. On restart, expired leases return to a safe pending state. Never infer success merely because an action was leased or submitted.

### 3.6 Make policy a first-class input

Route choice and transmission eligibility must consume an immutable policy snapshot. Policy includes automation mode, dry-run, priority, expiry, maximum hops, retry ceiling, airtime budget, duty-cycle windows, allowed groups, allowed band profiles, home-band/dwell rules, redundant-custody permission, retrieval cooldowns, and operator overrides.

Operator overrides should alter policy and append an audit event; they should not mutate historical decisions.

### 3.7 Treat online topology as an optional side channel

Design the shared service contract early, but keep it outside every RF-critical control path. The daemon must never wait for a network request before choosing, approving, or transmitting an RF action. It may consume already-cached shared evidence synchronously; refresh, upload, and health checks run as independent bounded background work.

The topology client may emit only normalized evidence carrying provenance, observation time, ingestion time, expiry, and confidence limits. It cannot emit a route decision or `TransmitIntent`. The local routing engine remains the sole route selector, and the local transmit arbiter remains the sole gateway to JS8Call.

“Fail quietly” means:

- DNS, TLS, timeout, server, and authentication failures never interrupt local RF work or produce repeated operator popups;
- failures are rate-limited in logs and exposed as a concise `offline`, `stale`, or `healthy` status in the UI;
- cached evidence ages normally and is excluded after expiry;
- upload failures use a short bounded retry buffer and then discard stale telemetry;
- emergency-priority routing never waits for online refresh and works identically with networking disabled.

Keep contribution and consumption independently switchable. The data plane should be disabled with one action and should close active requests immediately. A service endpoint configured as unreachable must be a normal, continuously tested operating condition.

### 3.8 Authenticate telemetry without pretending a bundled key is secret

An application key embedded in source or shipped in a binary is extractable and must be treated as a **public application token**, not a password. It can reduce casual garbage traffic and let the service apply client-version quotas, but it cannot authenticate a station, authorize privileged operations, protect uploaded data, or prevent a determined bot from replaying requests.

Use layered controls:

- ship a public app token only for coarse service identification and rate policy;
- generate a random per-install credential on first opt-in, store it locally with restrictive permissions, and allow the service to revoke it;
- sign each upload with an HMAC over method, endpoint, timestamp, nonce, body hash, and credential ID;
- use TLS with normal certificate validation and reject stale timestamps/replayed nonces;
- keep server-side quotas, payload-size limits, schema validation, abuse detection, and revocation independent of the token;
- never upload message content, credentials, audio, or private notes;
- never put a telemetry credential, signature, or online response on the RF path;
- do not log tokens, signatures, or full telemetry request bodies.

If a deployment cannot support per-install enrollment, the public app token may be the fallback, but the service must explicitly operate it as anonymous/untrusted telemetry with aggressive quotas. The UI disclosure should say that the token deters casual misuse only; it does not provide meaningful secrecy or station authentication. Endpoint configuration must support a self-hosted service with its own app token and credential policy.

## 4. Preliminary API capability matrix

Create the authoritative matrix in `docs/API_CAPABILITY_MATRIX.md` before radio-capable implementation. Each row must carry four independent statuses:

1. documented for version;
2. present in inspected source;
3. observed in a live installed build;
4. covered by an adapter contract test.

Use `supported`, `unsupported`, `degraded`, and `unknown`; never use truthy version comparisons as a substitute for probing.

The following preliminary matrix is enough to shape the work but is not the final runtime-verified artifact:

| Requirement | API/event | 2.3.1 / local 2.4 source | 3.0 docs | Planned use or fallback |
|---|---|---|---|---|
| Keepalive | `PING` | Present | Present | Connection health only; no response is expected. |
| Frequency/offset | `RIG.GET_FREQ` → `RIG.FREQ` | Present | Present | Snapshot and asynchronous updates. |
| Set offset/frequency | `RIG.SET_FREQ` | Present | Present | Disabled by default; only with explicit policy and verification. |
| PTT activity | asynchronous `RIG.PTT` | Present | Present | Track observed PTT edges. Startup state remains unknown until observed on older builds. |
| Query PTT | `RIG.GET_PTT` | Not in local 2.4 source | 3.0 | Improve arbitration when available. |
| Emergency halt | `RIG.TX_HALT` | Not in local 2.4 source | 3.0 | Use when supported. Older builds require local scheduler pause plus guided JS8Call stop; do not pretend remote halt exists. |
| Station identity | `STATION.GET_CALLSIGN` | Present | Present | Required startup probe. |
| Grid/info/status | `STATION.GET_*` | Present | Present | Read-only startup snapshot. |
| Exact application version | `STATION.VERSION` | Not in local 2.4 source | 3.0 | Otherwise record package/source knowledge as a hint and probe commands individually. |
| Station status stream | `STATION.STATUS` | Present | Present | Frequency, offset, speed, selection; not proof of idle/PTT on older builds. |
| Raw receive activity | `RX.ACTIVITY` | Emitted | Emitted | Channel activity and passive evidence. Bound ingestion rate and payload size. |
| Directed messages | `RX.DIRECTED` | Emitted | Emitted | Primary parser input for messages, commands, ACKs, heartbeats, and enhanced frames. |
| Directed-to-me event | `RX.DIRECTED.ME` | Code is commented out in local 2.4 source | Do not rely on it | Filter `RX.DIRECTED` locally. |
| Heard stations | `RX.GET_CALL_ACTIVITY` | Present | Present | Targeted snapshot after connection; observations still come primarily from passive events. |
| Band activity | `RX.GET_BAND_ACTIVITY` | Present | Present | Busy-channel evidence and restart warm-up. |
| Free offset selection | `RX.GET_FREE_OFFSETS` | Not in local 2.4 source | 3.0 | On older builds, remain on configured offset or require operator selection; do not invent collision-free offsets. |
| Current RX text | `RX.GET_TEXT` | Present | Present | Diagnostics/reconciliation only, never the canonical inbox. |
| Manual compose protection | `TX.GET_TEXT` → `TX.TEXT` | Present | Present | Mandatory preflight; non-empty or unknown means defer. |
| Queue message | `TX.SEND_MESSAGE` | Present, no direct completion response | Present | Submit only through the arbiter after all checks. Correlate later evidence; submission is not transmission. |
| Set compose text | `TX.SET_TEXT` | Present | Present | Avoid in normal automation because it touches operator-visible text. Keep adapter capability but policy-disabled. |
| Queue depth | `TX.GET_QUEUE_DEPTH` | Not in local 2.4 source | 3.0 | Older builds permit at most one JS8Mail in-flight submission and require conservative settling. |
| TX frame evidence | `TX.FRAME` | Emitted | Emitted | Count observed frames, redacting tone arrays in normal logs. |
| Speed read/set | `MODE.GET_SPEED`, `MODE.SET_SPEED` | Present | Present | Read in MVP. Setting remains operator-approved until live safety tests pass. |
| Local JS8Call inbox | `INBOX.GET_MESSAGES`, `INBOX.STORE_MESSAGE` | Present | Present | Reconcile local JS8Call inbox/store records; not equivalent to querying a remote custodian over RF. |
| Closing event | `STATION.CLOSING` | Not in local 2.4 source | 3.0 | Treat socket close as authoritative fallback. |
| API errors | `API.ERROR` | Present | Present | Persist bounded/redacted diagnostics and fail the correlated action safely. |

Important safety conclusion: on the 2.3/2.4 baseline there is no verified atomic “send only if idle and compose box empty” operation, queue-depth query, PTT query, or remote halt command. The arbiter must therefore be conservative:

1. require a listen-only settling interval after connect;
2. require a recent known-empty `TX.TEXT` response;
3. require no observed PTT, recent manual TX, queued JS8Mail attempt, or conflicting status;
4. require channel-idle evidence for the configured guard window;
5. recheck immediately before submission;
6. submit only one JS8Mail action;
7. watch `RIG.PTT`, `TX.FRAME`, socket state, and subsequent directed responses;
8. classify ambiguity as unknown/failed-safe, never success;
9. make Approve mode the first radio-capable default.

## 4.1 Local configuration versus peer capability

On first-run setup, JS8Mail should inspect the local JS8Call configuration and offer a guided, explicitly approved configuration profile. Helpful local settings may include the API, heartbeat networking, relay/store-and-forward, inbox facilities, subscribed groups, and decoding of the speeds the operator wants to monitor. Programmatic edits are allowed only when the setting and API support are verified for that exact build; otherwise the UI shows the precise JS8Call setting and asks the operator to change it.

Local configuration is never treated as a network-wide assumption. For every station and band/speed context, maintain separate facts for:

- heard/decoded at that speed;
- responded at that speed;
- successfully received an earlier part at that speed;
- acknowledged, forwarded, or store-accepted at that speed;
- explicitly advertised capability, if the enhanced protocol provides one;
- unknown capability.

For example, a Turbo query such as “has anyone heard G0ABC?” can be sent only when the selected target set has adequate Turbo evidence, or it must use a compatibility-safe strategy. The sender must never assume that a relay decoded one speed merely because our own station can decode or transmit it.

Speed selection is per attempt and per hop. A path may use Normal for one hop and Fast for another only when each hop has independent evidence; otherwise choose a conservative common speed or defer. A path-discovery query should prefer a broadly supported speed, use the smallest promising target set, and record that lack of response is ambiguous when decode speed is unknown.

The API capability matrix concerns the local JS8Call process. Peer speed capability is a separate observation model and must never be filled from the local matrix.

## 5. Target architecture

```text
Browser on loopback
       |
 HTTP + WebSocket/SSE
       |
 js8maild ---------------------------------------------------+
 | API/UI projection                                         |
 | application commands + queries                            |
 | durable scheduler -> policy -> radio-context arbiter      |
 |                                  -> transmit arbiter       |
 | message/custody/receipt state machines                     |
 | routing engine <-> observation/temporal graph              |
 | JS8Call adapter <-> normalized event bus                   |
 | audit + structured logging                                 |
 +----------------------------+-------------------------------+
                              |
                         SQLite WAL
                              |
                      migrations/backups

 JS8Call adapter -- TCP JSON --> unmodified JS8Call --> radio

 Optional phase-3 topology client -- HTTPS --> advice service
 (never connected to either arbiter)
```

Suggested repository layout:

```text
pyproject.toml
README.md
docs/
  IMPLEMENTATION_PLAN.md
  API_CAPABILITY_MATRIX.md
  MESSAGE_STATE_MACHINE.md
  DATABASE_SCHEMA.md
  PROTOCOL_V1.md
  SIMULATOR_TEST_PLAN.md
src/js8mail/
  __main__.py
  config.py
  domain/
    identifiers.py
    messages.py
    attempts.py
    custody.py
    receipts.py
    observations.py
    policy.py
    events.py
  application/
    commands.py
    queries.py
    lifecycle.py
    scheduler.py
    routing.py
    retrieval.py
    tx_arbiter.py
  adapters/
    js8call/
      client.py
      protocol.py
      capabilities.py
      normalizer.py
      validation.py
    sqlite/
      connection.py
      repositories.py
      migrations/
    topology/
      client.py
  api/
    app.py
    models.py
    events.py
  ui/
    templates/
    static/
  simulator/
    clock.py
    js8call_peer.py
    scenarios.py
tests/
  unit/
  contract/
  integration/
  simulation/
  fixtures/events/
```

Dependency direction is inward: UI/API and adapters depend on application/domain interfaces; domain and routing code know nothing about FastAPI, sockets, SQLite, or JS8Call JSON.

## 6. State-machine design deliverable

Before implementing scheduler behavior, write `docs/MESSAGE_STATE_MACHINE.md` with transition tables, triggers, guards, persisted side effects, and recovery rules. Generate diagrams from text in the repository so they stay reviewable.

### 6.1 Message lifecycle

Proposed states:

- `DRAFT`
- `QUEUED`
- `PLANNING`
- `WAITING_ROUTE`
- `WAITING_APPROVAL`
- `WAITING_OPPORTUNITY`
- `IN_PROGRESS`
- `HELD`
- `DELIVERED`
- `FAILED`
- `EXPIRED`
- `CANCELLED`

`DELIVERED` requires an enhanced destination delivery receipt or explicit operator confirmation for legacy recipients. Legacy automatic outcomes stop at an accurately worded strongest-known state such as “transmitted,” “hop acknowledged,” or “stored by N0CALL”; those are attempt/custody facts, not message delivery states.

### 6.2 Attempt lifecycle

Proposed states:

- `PLANNED`
- `WAITING_APPROVAL`
- `AUTHORIZED`
- `WAITING_CHANNEL`
- `SUBMITTING`
- `SUBMITTED_UNKNOWN`
- `PTT_OBSERVED`
- `FRAMES_OBSERVED`
- `ON_AIR_COMPLETE`
- `WAITING_RESPONSE`
- `HOP_ACKNOWLEDGED`
- `CUSTODY_ACCEPTED`
- `END_TO_END_RECEIPT`
- `TIMED_OUT`
- `REJECTED`
- `FAILED`
- `CANCELLED`
- `SUPERSEDED`

Some are evidence milestones rather than mutually exclusive facts. The detailed design may therefore use a compact attempt state plus timestamped evidence flags. The transition specification must decide this before schema migration 001 is frozen.

### 6.3 Mandatory invariants

- Terminal message states are monotonic except an explicit operator-created retry, which creates a new attempt and an audit event rather than erasing history.
- Every state transition and rejected transition is appended to the audit log in the same database transaction as the current-state projection.
- Replaying the same normalized event is idempotent.
- A duplicate receipt cannot duplicate an inbox message or advance an unrelated attempt.
- Expiry prevents new RF work but does not erase evidence or block processing of a late valid receipt.
- Cancellation prevents future scheduling; if RF submission is already ambiguous, the UI must say cancellation cannot recall an on-air or internally queued frame.
- Custody can move only on explicit, correlated evidence.
- Replanning starts from the last proven custodian for enhanced peers; origin retransmission requires policy justification and deduplication support.

## 7. SQLite schema design deliverable

Write the complete schema and indexes in `docs/DATABASE_SCHEMA.md`, then implement numbered forward migrations. Use UTC integer milliseconds, foreign keys, `STRICT` tables where available, CHECK constraints for bounded enums, and explicit uniqueness constraints for idempotency.

Initial entities:

| Table | Purpose and key constraints |
|---|---|
| `schema_migrations` | Applied migration version and checksum. |
| `config_entries` | Typed local settings with revision; secrets are references, not plaintext where avoidable. |
| `stations` | Canonical normalized callsign, optional openly heard grid, first/last seen. Unique callsign. |
| `station_sessions` | Derived availability windows by station/band/speed. |
| `observations` | Append-only normalized evidence with source, destination, RF context, provenance, confidence input, and raw-event hash. Deduplicate by source event identity/hash. |
| `temporal_links` | Rebuildable projection of directed edges and historical buckets; never the only copy of evidence. |
| `messages` | Stable message ID, sender/destination, kind, subject/body, priority, expiry, policy snapshot, lifecycle state, timestamps. |
| `message_parts` | Enhanced multipart payloads, part number/count/hash, receive state. Unique `(message_id, part_no)`. |
| `route_plans` | Candidate/selected path, score components, evidence snapshot, explanation, creation/expiry. |
| `attempts` | One execution attempt with action type, route, state, planned/actual timing, JS8Call correlation data, and strongest proven outcome. |
| `attempt_hops` | Ordered per-hop state/evidence; unique `(attempt_id, hop_index)` and loop-free validation in domain code. |
| `custody_events` | Append-only offered/accepted/transferred/released/expired custody facts. |
| `receipts` | Type, message ID, sender, correlation, protocol version, received/sent time; unique semantic receipt key. |
| `peer_capabilities` | Peer/protocol version/features, evidence source, observed time, expiry, next-probe time. |
| `groups` | Existing JS8Call groups and conservative forwarding/ACK policy. |
| `subscriptions` | Local group subscription state. |
| `scheduled_actions` | Durable due actions, lease, idempotency key, policy and reason. |
| `scheduler_decisions` | Candidate actions, chosen action, score/explanation, evidence ages, policy snapshot. |
| `airtime_ledger` | Estimated and observed airtime debits by rolling-budget scope. |
| `band_profiles` | Operator-approved bands and dial/offset profiles, antenna capability, home-band flag, and hopping policy. No inferred band is automatically allowed. |
| `radio_dwell_intervals` | Proven intervals during which this receiver was observing one band/frequency/speed context. Required to distinguish “not heard” from “not observed.” |
| `radio_context_actions` | Planned/approved/completed/failed band or frequency changes, previous context, reason, lease, and rollback result. |
| `audit_events` | Append-only domain/audit stream with redacted payload and causal/correlation IDs. |
| `raw_api_events` | Optional bounded diagnostic capture, disabled or aggressively redacted by default. |
| `topology_cache` | Phase-3 downloaded recent evidence and historical aggregates with endpoint/provenance/observation age/ingestion age/expiry. |
| `telemetry_outbox` | Phase-3 bounded, expiring upload batches containing only permitted fields and no RF-critical work. |

Database rules:

- one writer task owns write transactions; read connections may be separate;
- enable foreign keys, WAL, and a deliberate synchronous policy;
- transitions update projection rows and append audit events atomically;
- no SQL strings from RF/API input;
- bodies are excluded from ordinary logs, route explanations, and topology tables;
- migration tests run from an empty database and every previously released schema fixture;
- use SQLite online backup API for snapshots, with retention and operator export;
- startup runs integrity checks appropriate to startup cost and reports recovery options without silently discarding data.

## 8. Enhanced envelope design work

Do not freeze syntax from visual intuition. Produce `docs/PROTOCOL_V1.md` and a small frame-count experiment against the inspected JS8Call varicode implementation before implementation.

### 8.1 Protocol goals

- human-recognizable and valid as ordinary JS8Call directed text;
- compact stable message ID with enough collision resistance for local generation volume;
- explicit protocol major version;
- type field for capability, data, delivery receipt, custody receipt, read receipt, and negative response;
- multipart `part/total` and payload integrity check;
- duplicate suppression and idempotent receipt processing;
- source and destination remain visible in normal JS8Call addressing;
- no encryption or compression in v1;
- unknown/legacy recipients receive plain human-readable mail without enhanced framing unless explicitly forced for testing.

JS8Mail traffic has two deliberately different classes:

- **Control metadata**—capability announcements, message IDs, part numbers, selective ACKs, custody, and delivery/read receipts—must carry an unmistakable compact `J8M1` marker. Its fields may be optimized for airtime and do not need to be pleasant prose, but they must remain bounded, parseable, versioned, and visible to the operator.
- **Message data**—the subject/body content presented to the user—must remain human-readable. JS8Mail may add a small correlation prefix such as the provisional `J8M1 D <MID> <part>/<total>`; it must not replace the body with an opaque binary or application-compressed payload. Pass readable text to JS8Call and let JS8Call apply its own token/varicode encoding.

This means “compressed” in the airtime discussion refers to JS8Call's underlying encoding of readable text, not a hidden JS8Mail compression layer. The frame-count experiment must measure the actual JS8Call output for representative body text and separately measure compact metadata overhead.

### 8.1.1 Multipart loss and selective retransmission

The receiver persists each validated part keyed by `(message_id, part_number)` and deduplicates repeats. After a new part, it may send a rate-limited selective-part acknowledgement containing the total count and a received bitmap, or an equivalent missing-part list once the final wire grammar is ratified. The sender retransmits only missing parts, subject to normal retry, airtime, expiry, route, and duty-cycle policies.

Rules:

- a partial message is exposed as an explicitly incomplete preview when policy permits, especially for emergency traffic, but is never exposed as a complete inbox item;
- partial previews show received sections in order plus visible `[MISSING PART n/total]` markers and a pending status;
- a duplicate part is harmless and does not trigger an ACK storm;
- an inconsistent total, invalid position, oversized part, bad integrity check, or unknown message ID is rejected safely;
- a missing-part ACK is evidence/request only, not proof that retransmission succeeded;
- the receiver repeats its missing set after a cooldown if parts remain missing;
- the sender stops after bounded attempts or expiry and shows the exact missing parts;
- when all parts pass validation, the receiver atomically promotes one reassembled item and emits one delivery receipt for the message ID;
- late duplicate parts and duplicate delivery receipts remain idempotent;
- read receipts remain opt-in and separate from delivery receipts.

The current implementation contains these semantics in `js8mail.protocol.MultipartAccumulator`; its `J8M1 PA` formatter is provisional and must not be used on air until the envelope experiment and protocol review ratify the syntax.

### 8.1.2 Delivery confidence presentation

Use an evidence label with an explanation rather than a misleading probability. The UI may render a visual meter, but the canonical text must say what is proven:

| Evidence | Legacy recipient | Enhanced recipient |
|---|---|---|
| Submitted to JS8Call | Submitted; RF outcome unknown | Submitted; RF outcome unknown |
| Local TX frames observed | Frames observed; destination not confirmed | Frames observed; destination not confirmed |
| Hop ACK | Hop acknowledged; end-to-end delivery not proven | Hop acknowledged; end-to-end delivery not proven |
| All parts received | Not available automatically | Parts complete; delivery receipt pending |
| Destination delivery receipt | Not available automatically | Delivered to JS8Mail destination |
| Read receipt | Not available automatically | Read by destination operator, if opted in |

“Confidence” is the strength of evidence, not a statistical probability. The UI must show missing part numbers, last receipt time, retry count, current custodian, and expiry for incomplete multipart messages.

### 8.2 Candidate grammar to measure, not yet ratify

```text
J8M1 <TYPE> <MID> [<PART>/<TOTAL>] <PAYLOAD> [*<CHECK>]
```

Keep the marker and field alphabet within JS8Call's efficiently encoded character set. Candidate type tokens should be one character. Decide whether the integrity check is needed per part, whole message, or both only after measuring its cost and failure modes. A checksum detects corruption; it is not authentication.

Capability probing should normally be passive: a received enhanced envelope or receipt proves support. An active capability query gets a per-station expiry/cooldown and is sent only when a queued delivery would benefit.

### 8.3 Airtime experiment

Create a deterministic tool that invokes or faithfully ports only the frame-counting behavior needed to answer:

- frames for representative legacy text;
- incremental frames added by marker, message ID, type, part counters, and checksum;
- frames at Normal/Fast/Turbo/Slow;
- directed-call and relay-path overhead;
- delivery/custody/read receipt cost;
- worst-case multipart overhead;
- effect of common versus pathological character choices.

The inspected 2.4 source uses 79-symbol frames and nominal periods of Normal 15 s, Fast 10 s, Turbo 6 s, and Slow 30 s; Ultra 4 s exists in source but is disabled. Airtime must be calculated from actual encoded frame count and observed PTT timing, not character count alone, because the varicode is content dependent. Report both keyed airtime and wall-clock exchange time including required response windows.

Ratification gate: choose the shortest grammar that retains unambiguous parsing, protocol negotiation, deduplication, multipart integrity, and receipt correlation. Publish examples and strict maximum parser bounds.

## 9. Routing and retrieval design

### 9.1 Evidence hierarchy

Use this strict precedence:

1. current local RF evidence;
2. local historical evidence;
3. shared recent evidence;
4. shared historical evidence.

Shared evidence may add candidates and scores but cannot authorize RF or select the final route independently.

### 9.2 Temporal graph

Represent a potential link as a directed, context-dependent edge:

```text
(from, to, band_bucket, UTC_time_bucket, speed, evidence_type)
```

Keep raw observations. Build link/session aggregates as reproducible projections. Route scoring should expose components rather than one opaque number:

- weakest-link confidence;
- age of weakest/oldest supporting evidence;
- current endpoint availability;
- expected frame airtime;
- hop count penalty;
- demonstrated success/custody reliability;
- retry/path-history penalty;
- expiry feasibility;
- policy and budget eligibility.

Use a time-dependent bounded path search over a small candidate graph, not flooding. Reject repeated callsigns before scoring. Keep attempted path fingerprints per message. DIRECT, RELAY_NOW, STORE, and DEFER are explicit decision outputs with a plain-language explanation.

### 9.3 Custodian scoring

Do not reuse relay score for store decisions. Custodian score emphasizes observed uptime/session regularity, recurrence by UTC/band, prior store acceptance, successful later forwarding/retrieval, and failure/empty-query history.

Legacy recipients default to one custodian. Controlled redundant custody requires an enhanced destination able to deduplicate and an explicit policy limit.

### 9.4 Retrieval scheduler

Trigger targeted retrieval evaluation on:

- end of configurable listen-only startup interval;
- known custodian reappearance;
- heartbeat or message-waiting evidence;
- prior store transaction suggesting mail may exist;
- restrained periodic due time.

Maintain per-custodian cooldown, exponential backoff, jitter, empty-result count, last success, and next eligible time. Prefer recent proven custodians. A broad group query is a separately budgeted last resort. The simulator must prove the query rate converges downward after empty results.

### 9.5 Retention tiers

A single retention duration does not serve both privacy/storage bounds and seasonal prediction. Use configurable tiers, with these conservative defaults as the starting hypothesis to validate against real data volume:

| Data class | Local default | Shared-service default | Reason |
|---|---:|---:|---|
| Detailed normalized RF observations | 7 days | 30 days | Enough for recent route evidence while bounding callsign-level detail. |
| Raw API diagnostic events | Off by default; 24 hours when enabled | Never uploaded | Debugging only; message content is redacted and storage is tightly bounded. |
| Station sessions and link outcome detail | 30 days | 90 days | Supports custodian reliability and recurring weekly patterns. |
| Hour-of-week/band/speed aggregates | 13 months | 13 months | At least one annual cycle is needed to make “seasonal” evidence meaningful. These contain no message content. |
| Downloaded shared recent evidence | 7 days or server expiry, whichever is shorter | N/A | Useful offline cache without turning the client into a mirror. |
| Downloaded shared historical aggregates | 90 days of locally cached results, refreshed opportunistically | N/A | Bounds client storage; the server remains the longer historical source when reachable. |
| Unsent telemetry | Maximum 6 hours by default | N/A | Prevents a delayed bulk dump after an outage. Stale batches are deleted, not retried later. |

Retention applies to routing observations, not the user's mailbox or audit requirements. Messages, receipts, attempts, custody facts, and audit events use separate operator-visible retention/export policy and must never disappear merely because topology evidence aged out.

Aggregation is a scheduled local operation performed before detailed rows expire. Aggregates keep counts, success/failure rates, SNR distributions, availability windows, speed, band, and time bucket; they contain no subject, body, fragment, inbox content, or private note. They retain source class (`local` or `shared`) so evidence precedence remains enforceable.

The 7-day/30-day/13-month values are defaults, not protocol constants. Add storage-size ceilings as well as age limits, expose estimated disk use, and test pruning transactionally so deletion cannot block RF processing for long periods.

### 9.6 Optional multi-band operation

Keep Phase 1 operationally single-band, with 20 m as the normal home band for this installation, but make all RF evidence and route APIs band-aware from the first migration. Adding automatic hopping to the MVP would increase race conditions, reduce predictable receive availability, and make route failures much harder to interpret before the single-band state machine is proven.

Use three staged capabilities:

1. **Single-band** — observe and transmit only in the current JS8Call context. A route on another band is shown as unavailable advice, never acted upon.
2. **Guided/approved QSY** — JS8Mail recommends a configured band and explains why. The operator changes it, or approves an adapter action when safely supported. JS8Mail verifies the resulting `RIG.FREQ` before planning RF.
3. **Automatic hopping** — explicit opt-in after guided QSY is reliable. A durable radio-context scheduler may switch only among operator-approved profiles and must return to the home band according to policy.

An allowed band is more than a band name. Each `band_profile` should include:

- enabled flag and home/preferred rank;
- operator-selected JS8Call dial frequency and offset, not a hard-coded global frequency;
- antenna/feeder/tuner suitability confirmation;
- allowed receive and transmit use (receive-only profiles are valid);
- permitted speeds;
- local regulatory/operator notes without claiming the software can determine legal authority;
- minimum dwell, maximum hops per hour, optional UTC schedule, and return-to-home policy;
- whether each change requires approval.

The current band/frequency remains owned by the operator. JS8Mail must detect manual changes, cancel stale band-change plans, and adopt the new context without fighting the operator.

#### Observation opportunity and absence

Record a `radio_dwell_interval` whenever connection, dial/offset, or speed context changes. Negative evidence is valid only if this receiver was connected and dwelling on the relevant band during the relevant opportunity window. Never reduce a station's reachability or availability score because it was not heard while the receiver was on another band, disconnected, transmitting, or inside a known decode gap.

Route explanations should therefore say either:

- “not heard on 20 m during 43 minutes of local listening,” or
- “no recent local observation opportunity on 40 m,”

rather than treating both as “not heard.” Shared evidence can suggest a 40 m path while the local client is on 20 m, but it remains lower-confidence until locally observed or deliberately tried.

#### Receive-availability trade-off

Hopping creates a rendezvous problem: a station cannot receive 20 m mail while listening on 40 m. Mitigate it with:

- a home band where the station spends most idle time;
- minimum dwell windows long enough to observe several JS8 cycles and restrained heartbeats;
- bounded exploratory time away from home;
- route/receipt/retrieval locks that keep the radio on the active band's context;
- return-to-home after the lock or timeout;
- optional operator schedules based on established operating practice;
- custody/store-and-forward rather than constant scanning when possible.

Do not hop merely because another band has a slightly higher historical score. Require a configurable improvement margin that includes switch cost, loss of home-band listening, evidence age, expected dwell, and message expiry. Normal-priority mail may defer for a scheduled opportunity; emergency priority may request an earlier approved change but cannot bypass antenna, regulatory, airtime, or manual-control constraints.

Legacy stations cannot be expected to follow coordinated QSY instructions. Enhanced peers may eventually negotiate a compact rendezvous window over the current band, but that is a post-v1 protocol extension and must have timeout/fallback behavior. Initial multi-band delivery should use independent evidence that the destination/custodian is normally active on the target band.

#### Radio-context arbiter

Band/frequency changes do not go through the transmit arbiter as RF messages, but they compete for the same radio resource. Add a parent `RadioContextArbiter` that serializes manual-state observation, QSY, settling, listening, and transmit leases. A QSY is forbidden while:

- JS8Call is or may be transmitting;
- manual compose text or operator activity is present;
- an attempt is awaiting an immediate hop ACK/receipt window;
- a multipart transmission or custody transaction is incomplete;
- frequency-setting capability is unverified;
- the target is outside the enabled operator allowlist.

After a change, verify dial and offset from the API, wait through a configurable rig/decoder settling interval, start a new dwell interval, and require fresh channel evidence before any transmit. On mismatch or socket loss, stop automation and report the actual observed context; never repeatedly force the target frequency.

## 10. Transmission arbiter

All RF-producing application paths—including mail, capability exchange, receipts, retrieval queries, group actions, retries, and emergency messages—must submit a `TransmitIntent` to one arbiter.

A transmit intent contains exact on-air text, destination/path, purpose, priority, estimated frames/airtime, expiry, required capability, approval token, idempotency key, and evidence/policy snapshots.

Preflight checks, in order:

1. automation is not paused and mode permits the action;
2. dry-run either records the hypothetical attempt or permits continuation;
3. intent is not expired/cancelled/superseded;
4. text, callsigns, path, groups, lengths, and command syntax validate;
5. capability requirements are satisfied;
6. retry, hop, route-loop, duty-cycle, and airtime budgets permit it;
7. JS8Call connection is stable past the settling interval;
8. local JS8Mail has no unresolved submission;
9. JS8Call PTT/queue/manual text state is known idle to the strongest available level;
10. channel has remained idle for the guard interval;
11. randomized polite backoff has elapsed without new activity;
12. approval is current and matches the exact text;
13. immediately repeat volatile checks;
14. durably mark `SUBMITTING`, then call the adapter once.

The kill switch first prevents all new submissions and cancels unleased scheduler actions. If `RIG.TX_HALT` is verified, it may also halt current JS8Mail-initiated transmission. On older builds, clearly state that a daemon pause cannot remotely retract frames already queued inside JS8Call.

Never use `TX.SET_TEXT` as the normal automated send path. Never change speed or frequency while JS8Call reports or may be transmitting. Manual operator activity always wins.

## 11. Simulator and test strategy

Write `docs/SIMULATOR_TEST_PLAN.md` before implementing route automation.

### 11.1 Simulator components

- virtual monotonic and UTC clocks;
- fake JS8Call TCP server with configurable API capability profile;
- strict newline-delimited JSON behavior plus fragmentation/coalescing/malformed-input cases;
- event recorder/replayer with redacted fixtures;
- deterministic RF network model containing stations, directed temporal links, SNR, speed, loss, delay, and availability windows;
- fake optional topology service;
- deterministic random source for jitter/backoff/path tie-breaking;
- crash points before/inside/after each durable transition and adapter submission.

Production scheduling code must accept clock and random interfaces; tests must never sleep in real time.

### 11.2 Test layers

| Layer | Focus |
|---|---|
| Unit | parsers, validation, IDs, state transitions, scoring, budgets, backoff, frame estimates. |
| Adapter contract | request/response correlation, event normalization, capabilities, reconnects, framing, bounds, version differences. |
| Database | migrations, constraints, transaction atomicity, backup/restore, duplicate events, lease recovery. |
| Protocol | golden vectors, malformed/truncated frames, duplicates, multipart reorder, receipt correlation, version negotiation. |
| Integration | daemon + fake JS8Call + SQLite + local API/UI projections. |
| Deterministic simulation | temporal routes, failures, restart points, congestion, retrieval behavior, optional server loss. |
| Hardware-in-loop/manual | receive-only first, dummy load or disabled PTT for TX validation, then tightly controlled RF acceptance. Never part of CI. |

### 11.3 Mandatory deterministic scenarios

- socket disconnect and reconnect during idle, planning, submission, and ACK wait;
- restart before submission, immediately after submission, during observed PTT, and after receipt before projection update;
- duplicate frames, duplicate receipts, multipart reorder, missing part, corrupt check, and protocol-version mismatch;
- selective missing-part ACK, duplicate-part suppression, ACK cooldown, retransmission ceiling, partial expiry, and atomic promotion of a complete message;
- late ACK from a prior attempt and ACK from the wrong station;
- stale direct route versus fresh relay evidence;
- three-hop path discovery and use;
- cyclic path candidate rejection;
- relay disappears before use and after custody transfer;
- safe replan from last proven custodian;
- legacy destination where strongest proof is hop ACK/store only;
- manual compose text appears at every arbiter race point;
- operator pauses or kills at every arbiter state;
- repeated empty custodian queries back off with bounded rate;
- group duplicate/rebroadcast suppression and randomized/designated ACK policy;
- retry ceilings, message expiry, duty-cycle limits, and no sustained saturation;
- optional topology server unavailable, slow, malicious, stale, or contradictory;
- absence on an unobserved band does not lower station confidence;
- guided QSY, operator override, failed QSY verification, reconnect during QSY, and safe return-to-home;
- hopping does not interrupt ACK/receipt/multipart windows and cannot starve the home band;
- a tempting shared-data route on a disallowed or antenna-incompatible band is rejected;
- malformed/oversized JSON and RF text remain bounded and inert.

## 12. Implementation sequence

Each milestone ends in a runnable demonstration and tests. Do not build the polished mailbox ahead of the durable core.

### Milestone 0 — evidence and contracts

Deliver:

- final API capability matrix for stock 2.3.1, local improved 2.4.0, and documented 3.x;
- receive-only live probe tool that cannot send RF commands;
- captured/redacted event fixtures;
- message/attempt/custody/receipt transition specification;
- schema document and migration 001 review;
- enhanced envelope experiment and airtime report;
- simulator/test plan;
- architecture decision records for UI transport, persistence, IDs, and protocol framing.

Exit gate:

- every required JS8Call event/action is verified, degraded with an explicit fallback, or marked unsupported;
- no unverified JSON field has escaped the adapter contract;
- protocol and schema invariants are testable;
- operator approves the proposed envelope before it becomes on-air compatibility surface.

### Milestone 1 — project skeleton and durable event core

Deliver:

- package layout, lint/type/test configuration, CI on Python 3.12+;
- configuration loading with safe defaults and no INI mutation;
- SQLite connection, migration runner, backup command, audit events;
- domain identifiers and immutable normalized events;
- evidence-source interface with required provenance/age/expiry fields, implemented initially by local RF and a disconnected fake topology source;
- fake clock/random and fake JS8Call server.

Exit gate:

- fresh install, migration, restart, backup, restore, and corrupt-input tests pass;
- CI requires no radio, GUI, Internet, or JS8Call installation.

### Milestone 2 — connection and passive capture vertical slice

Deliver:

- asyncio TCP client with bounded newline framing, reconnect/backoff, and status;
- startup read-only capability probes;
- normalization of `RX.ACTIVITY`, `RX.DIRECTED`, `RX.SPOT`, `RIG.FREQ`, `RIG.PTT`, `STATION.STATUS`, and API errors;
- station/session/observation persistence with local/shared provenance preserved from ingestion onward;
- minimal local status/station view;
- raw-event replay tool.

Exit gate:

- daemon can run indefinitely receive-only;
- socket loss and malformed/coalesced/fragmented events cannot crash or grow memory without bound;
- restart preserves observations and derives sessions deterministically.

### Milestone 3 — durable mailbox and dry-run planning

Deliver:

- drafts, inbox, outbox, sent, failed/expired projections;
- compose validation, stable message IDs, expiry, priority, and policy;
- message lifecycle and durable scheduler leases;
- conservative DIRECT/DEFER route decision from current local evidence;
- attempt timeline and exact on-air preview;
- dry-run arbiter with airtime ledger.

Exit gate:

- queued mail survives forced process termination at every persistence boundary;
- dry-run displays exact intended text, time, evidence, reason, and budget impact;
- no code path can reach a real send adapter while dry-run is active.

### Milestone 4 — approved safe transmission prototype

Deliver:

- single transmit arbiter;
- Approve mode and immediate pause;
- `TX.GET_TEXT`/PTT/channel preflight and one-at-a-time submission;
- observed frame/PTT timeline and accurate ambiguous outcomes;
- manual direct action first; manual relay/store only after syntax verification;
- cancel/retry controls with honest limitations.

Exit gate:

- fake-server race suite passes;
- receive-only hardware test passes;
- controlled non-radiating/dummy-load test verifies no overwrite or interruption of manual text;
- real RF activation requires an explicit configuration switch and confirmation;
- this is the requested “small vertical prototype.”

### Milestone 5 — complete offline Phase-1 MVP

Deliver:

- standard JS8Call direct, verified relay, and store operations;
- targeted startup/reappearance/message-waiting retrieval checks;
- local-current-evidence route candidates and simple multi-hop paths;
- bounded retries, jitter/backoff, TTL, hop/loop/path-history controls;
- Observe/Approve/Automatic modes, with Automatic opt-in;
- basic mailbox actions and explainable route/attempt views;
- diagnostic export with body redaction.

Exit gate:

- all offline MVP criteria that do not require the enhanced protocol pass in simulation and controlled integration;
- legacy recipients receive readable text;
- no status vocabulary overclaims delivery.

Phase-1 scope decision: the MVP is single-band. It records band-aware evidence and dwell intervals, displays routes suggested on other bands as advice, and notices manual QSY, but it does not initiate a band change.

### Milestone 6 — enhanced protocol foundation

Deliver:

- ratified protocol v1 parser/formatter and published vectors;
- passive capability detection plus rate-limited active probe;
- duplicate suppression and multipart reassembly;
- destination delivery receipts;
- optional read receipts, disabled by default;
- custody acknowledgements and transfer state.

Exit gate:

- two simulated instances and then two controlled JS8Mail instances exchange IDs and unambiguous end-to-end receipts without duplicate inbox entries;
- partial multipart delivery identifies missing parts, retransmits only those parts, and creates exactly one complete inbox item after reassembly;
- legacy confidence never exceeds hop acknowledgement, and enhanced confidence never exceeds parts-complete until an end-to-end receipt arrives;
- unknown versions fail safely and preserve human-readable legacy behavior where possible.

### Milestone 7 — robust temporal routing and groups

Deliver:

- historical time/band/session projections;
- separate immediate-relay and custodian scoring;
- bounded multi-hop temporal search and selective queries;
- replan from proven custodian;
- operator-configurable band profiles and home band;
- guided/approved QSY with verified context, dwell accounting, receive-opportunity semantics, and safe return-to-home;
- automatic hopping only as a separate opt-in capability after guided-QSY acceptance tests pass;
- speed reliability model and conservative recommendation/selection gates;
- existing-group subscriptions, bulletins, check-ins, emergency labels, TTL/dedup, and ACK suppression;
- automatic group forwarding remains explicitly opt-in.

Exit gate:

- all multi-hop, disappearing-relay, congestion, retrieval, group-storm, and adaptive-speed simulations pass;
- multi-band simulations prove operator priority, allowlist enforcement, context verification, home-band availability, and no false negative evidence from unobserved bands;
- route explanations expose evidence source and age.

### Milestone 8 — optional shared topology service (Phase 3)

Begin only after local-only reliability is demonstrated.

Deliver:

- separate contribute/consume switches and first-run disclosure;
- non-blocking background client implementing the evidence-source interface designed in Milestone 1;
- recent-observation and compact historical-aggregate endpoints; the response is evidence, never a final route;
- strict telemetry allowlist and tests proving message data cannot enter it;
- bounded expiring upload buffer, batching, and rate limits;
- 30-day service retention for detailed observations and 13-month retention for compact time/band/speed aggregates by default;
- downloaded cache with endpoint provenance, observation/ingestion age, expiry, and confidence decay;
- route candidate enrichment with local evidence precedence;
- configurable endpoint and quiet offline/stale behavior;
- health/status telemetry that is informative but cannot page, block, or repeatedly interrupt the operator.

Exit gate:

- disabling sharing stops it immediately;
- stale queued telemetry is discarded;
- service outage or malicious advice cannot initiate RF, bypass policy, or stop local routing;
- emergency-mode simulations with DNS failure, timeout, empty cache, and networking disabled produce the same local RF decisions and timing;
- no daemon RF-path coroutine awaits the topology service.

## 13. UI implementation order

Build only the views needed to exercise the current milestone:

1. daemon/JS8Call connection, frequency, speed, PTT/busy confidence, automation mode, dry-run, and kill switch;
2. station/session observations;
3. compose and outbox timeline;
4. inbox/sent/failed/drafts;
5. route evidence and attempted paths;
6. settings/setup guidance;
7. allowed-band profiles, home band, guided-QSY approval, dwell/schedule limits, and hopping opt-in;
8. group and shared-topology controls.

Use server-provided display strings for lifecycle semantics initially so the browser cannot independently reinterpret a hop ACK as delivery. Add accessibility, keyboard operation, and clear UTC/local-time labels from the start.

## 14. Security, privacy, and boundedness checklist

- Parse JSON into bounded typed models; reject excess nesting, oversized lines, oversized strings, impossible timestamps, and unknown enum values safely.
- Treat all RF text, callsigns, grids, API fields, topology responses, filenames, and export labels as hostile input.
- Never evaluate text or interpolate it into SQL, shell commands, HTML, logs, or paths.
- Escape UI output and use a restrictive content security policy.
- Keep daemon mutation endpoints loopback-only and protected against cross-site requests.
- Bound socket queues, event queues, database batches, retries, route candidate counts, graph search, multipart counts, message sizes, and diagnostic retention.
- Redact message subject/body/fragments and tone arrays from standard logs.
- Make audit data useful without copying private content.
- Store no RF message content in the optional service path.
- Use UTC internally, monotonic time for elapsed timers, and injectable clocks in tests.
- Avoid direct edits of JS8Call configuration. Guided setup names the exact UI setting and verifies it through read-only/runtime behavior where possible.

## 15. Acceptance traceability

Create a test ID for every acceptance criterion and link it from code/tests and the final matrix. Minimum top-level IDs:

- `AC-OFFLINE-001`: offline queue survives restart and supports direct/relay/store/defer;
- `AC-LEGACY-001`: unmodified recipient receives readable message;
- `AC-E2E-001`: enhanced ID and end-to-end receipt without duplicate inbox;
- `AC-STATUS-001`: hop ACK never renders as delivered;
- `AC-ARBITER-001`: manual activity wins and kill switch prevents new submission;
- `AC-RECOVERY-001`: disconnect/restart/late/duplicate/stale/server-loss scenarios;
- `AC-ROUTE-001`: three-hop path, cycle rejection, per-hop display, safe replan;
- `AC-RETRIEVAL-001`: targeted triggers and demonstrable backoff;
- `AC-BUDGET-001`: no retry, relay, group-ACK, or saturation storm.
- `AC-BAND-001`: single-band operation remains reliable with all hopping disabled;
- `AC-BAND-002`: unobserved-band absence is never scored as a failed hearing opportunity;
- `AC-BAND-003`: QSY uses only approved profiles, yields to manual control, verifies context, protects response windows, and returns home safely.

No milestone is complete merely because its UI path works. Its relevant acceptance IDs, crash points, and negative safety cases must pass deterministically.

## 16. First implementation handoff

The next model should begin with Milestone 0, not the daemon skeleton. Its first concrete changes should be:

1. add `docs/API_CAPABILITY_MATRIX.md` using source permalinks and a runtime-verification column;
2. add a receive-only `tools/probe_js8call_api.py` that has no transmit-capable request constants;
3. add `docs/MESSAGE_STATE_MACHINE.md` with exact transition tables and invariants;
4. add `docs/DATABASE_SCHEMA.md` with migration 001 SQL for review;
5. add a frame-count experiment and `docs/PROTOCOL_V1.md` with measured airtime;
6. add `docs/SIMULATOR_TEST_PLAN.md` with the acceptance traceability table;
7. stop for review before implementing anything that can key PTT.

This order intentionally resolves the highest-risk unknowns—the API safety envelope, delivery semantics, durable transitions, and on-air protocol cost—before they become expensive compatibility commitments.
