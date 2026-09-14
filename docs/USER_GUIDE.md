# JS8Mail user and operator guide

JS8Mail is a local, offline-first mail client and delivery scheduler that runs
above an unmodified JS8Call installation. JS8Call remains responsible for the
modem, decoding, audio, CAT/PTT control, JS8 token replacement, and the
ordinary directed-message, relay, heartbeat, hearing, and inbox facilities.
JS8Mail adds a durable mailbox, evidence collection, route selection, custody
tracking, enhanced-peer receipts, multipart recovery, and an operator-facing
web interface.

This document describes the current `0.0.3` implementation. It is useful and
radio-capable, but still early and experimental. In particular, a route score
is evidence-based advice, not a guarantee that a station is listening now.
Always operate within your licence, local band plan, power limits, and the
expectations of other operators.

## What JS8Mail is for

The primary use case is reliable text delivery when speed is less important
than eventual delivery. Examples include:

- normal low-priority mail that can wait for a good propagation opportunity;
- emergency or disaster communications when Internet services are unavailable;
- a message that should be left with a reachable station for later collection;
- a longer message sent to another JS8Mail station with part-level recovery;
- a group bulletin where the operator explicitly chooses the group and policy.

Internet access is not part of the RF delivery path. A future shared topology
service can contribute historical observations, but it can fail silently and
the local daemon must continue operating without it.

JS8Mail is not an encryption system, an authentication system, a replacement
for JS8Call, or a guarantee that a legacy station has received or read a
message. Message bodies remain readable on the air. Do not put confidential
material into an amateur-radio message.

## Architecture

The running system has three practical pieces:

1. `js8maild`, currently hosted by `js8mail.tools.app`, owns the asynchronous
   JS8Call TCP connection, scheduler, transmit pacing, routing, SQLite store,
   protocol handling, and audit events.
2. The local web UI talks to that daemon over HTTP on loopback. It does not
   speak to JS8Call directly.
3. JS8Call performs the actual RF work. JS8Mail submits bounded text through
   the documented local JSON API and listens to normalized events returned by
   that API.

The SQLite database is local. It contains messages, attempts, observations,
temporal links, session history, message parts, custody, capabilities, groups,
airtime counters, and audit events. UTC milliseconds are used internally.

## Installation and first start

Python 3.12 or newer is required for the current development build. From the
repository directory:

```sh
./start.sh --host 127.0.0.1 --port 2442 --ui-port 8765 --tx-mode observe
```

Then open <http://127.0.0.1:8765>. `start.sh` creates `.venv` when needed and
installs the project and test dependencies. It is safe to run again after a
source update.

For live automatic handoff, simply omit `--tx-mode` (automatic is the
default):

```sh
./start.sh --host 127.0.0.1 --port 2442 --ui-port 8765
```

The current command-line modes are:

| Mode | Behaviour |
| --- | --- |
| `observe` | Capture events and build evidence, but do not submit RF text. |
| `approve` | Reserved safety mode for an approval-gated workflow. |
| `automatic` | Submit eligible JS8Mail frames to JS8Call after the safety checks. |

The current UI is intentionally functional rather than visually elaborate. It
contains an inbox, compose form, recently heard stations, emergency/group
catalogue, outbox timeline, graph view, and recent observations. Queueing a
message scrolls the page to the outbox. Expanding an outbox item pauses full
mailbox redraws so the detail view does not collapse while reading it.

## JS8Call configuration

JS8Mail does not blindly edit JS8Call configuration files. Configure and test
JS8Call itself first, then start JS8Mail.

### Enable the local TCP/JSON API

In JS8Call, open the networking/API settings and enable the documented TCP
server/API. The usual local endpoint is:

- address: `127.0.0.1` (loopback);
- port: `2442`.

Use the exact port shown by your installed JS8Call build; pass it to
`start.sh --port`. Do not expose the API to an untrusted network interface.
The API is a control surface for the radio, not an Internet service.

The official API references are:

- [JS8Call API documentation](https://js8call.com/JS8Call-improved/d7/d15/md_docs_2API.html)
- [JS8Call upstream project](https://github.com/js8call/js8call)
- [JS8Call-improved source](https://github.com/JS8Call-improved/JS8Call-improved)

If the API is disabled, the probe will fail with `ConnectionRefusedError`.
That means JS8Mail is not listening on the port; it does not indicate an RF
problem.

### Audio and radio basics

Before enabling automatic mode:

1. Confirm JS8Call receives audio and displays decoded traffic.
2. Confirm JS8Call itself can transmit into a dummy load or other safe test
   setup.
3. Confirm the selected input and output devices are the intended interfaces.
4. Confirm the selected USB/serial CAT/PTT device and frequency.
5. Start JS8Mail in `observe` mode and check that the callsign, speed, RX/DCD/TX
   indicators, and observations update.

JS8Mail cannot repair a JS8Call audio or PTT configuration. If JS8Call enters
TX but no audio appears, troubleshoot JS8Call and PipeWire/ALSA independently
first. JS8Mail only sees the API-level events and cannot prove RF energy at
the transmitter output.

### Helpful JS8Call facilities

The following facilities improve the evidence available to JS8Mail, but an
intermediate or destination station may not have them enabled:

- heartbeat networking and normal heartbeat transmission;
- directed-message reception and decoding at the speeds you intend to use;
- relay and store-and-forward/inbox facilities;
- normal JS8Call groups relevant to your operating area;
- a stable, known JS8Call speed for initial testing;
- a sensible dial frequency and offset on the same shared JS8Call channel as
  the stations you are trying to reach.

JS8Mail never assumes that a remote station decodes Turbo or Ultra merely
because the local radio does. A missing response at a speed the remote station
may not decode is ambiguous evidence, not proof that the remote station is
absent.

### API capability differences

JS8Call versions differ in event names, read-only requests, queue visibility,
speed control, and emergency/halting commands. The adapter isolates these
differences. Unsupported or unverified commands are optional capabilities;
they must not be treated as available solely because a version string looks
new enough. See [`API_CAPABILITY_MATRIX.md`](API_CAPABILITY_MATRIX.md).

## The complete outgoing-message lifecycle

Suppose the operator composes a message to `G0ABC` and selects **Queue
locally**.

### 1. Durable queueing

The message is written to SQLite before RF work begins. It receives a stable
compact message ID, an expiry (currently three days by default), priority, and
an initial `queued` state. Restarting JS8Mail or losing power must not erase
the message.

### 2. Small direct reachability probe

The first automated action is deliberately small:

```text
G0ABC SNR?
```

This is not the message itself. It asks whether the destination can hear the
origin and gives the scheduler a current response opportunity. A timeout is
ambiguous: the station may be absent, asleep, on another band, decoding a
different speed, or simply not have heard that cycle.

The probe is recorded as an attempt. It is never displayed as delivery.

### 3. Interpret fresh evidence

Passive observations are preferred. JS8Mail records directed traffic,
heartbeats, activity, SNR, grid, speed, frequency/band, `YES` responses,
query-call responses, and relay/store activity when those fields are exposed
by JS8Call.

If the destination recently answered the local station, the next eligible
action is normally a direct ordinary message. This direct-first rule is
intentional: entering a destination manually should not unexpectedly relay
through a graph before giving the destination a short direct opportunity.

If the destination has not answered, JS8Mail does not immediately send the
whole message repeatedly. It queries selectively, waits for evidence, and
then considers routes or a custodian.

### 4. Selective discovery

Discovery proceeds from cheap and targeted evidence toward broader evidence:

1. use a fresh direct answer, if one exists;
2. use current local temporal-link evidence;
3. ask a small set of promising stations whether they can hear the destination;
4. if there are no promising candidates, use a rate-limited
   `@ALLCALL QUERY CALL G0ABC`;
5. after enough unsuccessful direct/discovery attempts, consider a custodian
   that is currently promising and not already holding the message.

The query scheduler has its own cooldown separate from each message's retry
timer. Therefore a message can become due while a particular query is still
cooling down; the UI may show that the query was skipped or blocked while the
message remains queued. This prevents a fleet of queued messages from turning
into a broadcast beacon.

`QUERY CALL` replies are deliberately compact. For example:

```text
OH3SPN: MM0ZFG QUERY CALL SP2ST
MM0ZFG: OH3SPN YES
```

The `YES` is addressed back to the requester and does not repeat `SP2ST`.
JS8Mail therefore correlates it with the recent outstanding query. A bare
`YES` is valid; an SNR and age such as `YES -06 (12M)` may also be present.
Only one destination query is kept outstanding for a particular station (or
for `@ALLCALL`) at a time, because overlapping queries would make a compact
reply ambiguous. A positive answer records both the successful interaction
with the reporting station and its remote evidence for the requested
destination, then immediately wakes matching queued mail for route planning.
It is evidence for a route attempt, not a guarantee that relaying, AUTO, or
store-and-forward is enabled at the reporting station.

`@ALLCALL QUERY MSGS` is treated as a broad inbox check and is intentionally
much slower—about every 30 minutes initially, with restrained backoff. Known
custodians are queried directly before resorting to broad polling.

### 5. Build the temporal graph

Every useful observation becomes evidence for a directed temporal link. For
example, if a received JS8Call event indicates that `RELAY1` heard or worked
`G0ABC`, JS8Mail may add:

```text
RELAY1 → G0ABC
```

The local graph is not a claim that JS8Mail controls either station. It is a
record of what was observed, when it was observed, on which band/speed when
available, and with what SNR or query age. Evidence is retained locally for
roughly a week in the detailed observation table; the online service is a
future optional extension for longer historical retention.

### 6. Score candidate links

The current route engine uses a deterministic score so a decision can be
explained. For each link:

1. SNR-derived evidence is converted into a bounded base score. Roughly, the
   implementation maps `-20 dB` to the middle of the usable range and allows
   very weak evidence to remain only a low-confidence candidate; strong
   evidence approaches `1.0`.
2. Evidence decays exponentially with age. Local observations decay over about
   15 minutes, remote query reports over about one hour, and historical link
   projections over about one day. Recent local evidence therefore beats old
   evidence, while history can still suggest where to ask next.
3. The strongest usable evidence for a directed pair is selected at planning
   time.
4. Reported age from a query response is subtracted from the observation time,
   so “heard one minute ago” does not look like a brand-new local observation.

The current implementation does not pretend that a single SNR number is a
probability of delivery. It is a comparable ranking signal used alongside
freshness, hop count, airtime, availability, and prior attempts.

### 7. Search complete paths

The engine walks the directed graph from the origin to the destination. It
rejects repeated callsigns while walking, so cyclic paths are not selected.
There is no small hard-coded hop limit in the current default planner; a long
acyclic route may be selected when its evidence remains above the minimum
score. Airtime, weak links, expiry, and retry policy still make very long
paths unattractive.

For each complete candidate path it calculates:

```text
path score ≈ weakest link score
              − hop penalty
              − expected airtime penalty
```

The weakest-link rule is deliberate. A path with three excellent links and
one very weak link should not outrank a shorter path whose weakest link is
solid merely because its average looks attractive. Expected airtime also
prevents a very long path from winning on weak historical evidence alone.

The highest-ranked untried path is selected first. If all viable paths have
been attempted, a previous path is allowed to re-enter consideration later:
propagation changes, so a failed path is cooled down rather than permanently
blacklisted.

### 8. Choose direct, relay, store, or defer

The planner maps its result to one of four practical actions:

- `DIRECT`: send the ordinary destination-addressed message;
- `RELAY_NOW`: send JS8Call-compatible relay text over a selected path;
- `STORE`: offer a readable message to one promising custodian for later
  retrieval by the destination;
- `DEFER`: do not spend payload airtime yet; collect more evidence and retry.

The current operational policy is conservative:

- first contact remains direct-first;
- no full payload is repeatedly sent to an entirely unverified destination;
- a custodian is considered after repeated discovery/direct failure;
- redundant custodians are not automatically used for ordinary legacy
  recipients, because they could create duplicate human messages;
- retries use exponential/time-aware backoff and stop at message expiry;
- all transmissions pass through one serialized transmit path.

### 9. Submit safely to JS8Call

Before submission JS8Mail validates callsigns, sizes, framing, and airtime.
The transmit path also checks that JS8Call is connected and that its current
transmit text is not occupied by the operator. It never overwrites manual
text. A single lock serializes automated submissions, and a polite gap is
left between JS8Mail handoffs so the daemon can hear replies and does not
stack several automatic requests into the same JS8Call opportunity.

The submission event means “JS8Call accepted text for its next opportunity.”
It does not prove that the frame was decoded by anybody.

## Route evidence and delivery states

The outbox deliberately distinguishes these states:

| UI meaning | What it proves |
| --- | --- |
| Submitted to JS8Call | Text reached the local JS8Call API. |
| Frames observed | JS8Call produced a TX frame; remote decoding is unproven. |
| Radio acknowledged / hop ACK | An addressed station acknowledged a hop; end-to-end delivery is unproven. |
| Stored at custodian | A custodian acknowledged a store operation; recipient retrieval is still pending. |
| Delivered / Complete | An ordinary/known delivery conclusion supported by local evidence. |
| Complete+ | A JS8Mail destination sent an end-to-end delivery receipt. |
| Read | Only available for an explicitly enabled read receipt. |
| Discovery in progress | No stronger conclusion is currently proven. |

The graph view uses these colours:

- grey: observed RF evidence;
- orange: an attempted message edge;
- green: a confirmed edge/receipt.

The graph is an evidence view, not a promise that every grey edge is an
available relay. A future UI improvement should separate “global observations”
from “paths actually attempted for this message” even more explicitly.

For enhanced multipart traffic, a plain ACK is intentionally weaker even when
it comes from the final callsign: it confirms a JS8Call hop, not complete
reassembly by the JS8Mail client. The sender waits for `J8M1 DELIVERED`. If the
destination reports a missing-part bitmap, JS8Mail retransmits only those
parts, using the recorded reverse path when available. If a final receipt
contains a known accepted custodian, the outbox also records that custodian as
forwarded. Standard JS8Call relay/store remains the compatibility transport;
an intermediate station is not assumed to run JS8Mail merely because it carries
an opaque `J8M1` frame.

## JS8Mail enhanced peers

The first message to an unknown station remains ordinary readable JS8Call text
plus a separate capability advertisement when airtime permits:

```text
J8M1 CAP 1 E2E,MP,PA
```

Capabilities are cached for seven days. A peer is not treated as enhanced just
because the destination string looks familiar or because a third station has
JS8Mail.

Enhanced features are:

- stable message IDs and duplicate suppression;
- numbered human-readable parts;
- selective part acknowledgements;
- resend requests for missing parts;
- end-to-end delivery receipts with delivery time and bounded path metadata;
- optional read receipts, separate from automatic delivery receipts;
- custody and forwarded-receipt correlation.

New enhanced transfers use the origin-aware part form
`J8M1 D <ORIGIN> <DEST> <MID> <PART>/<TOTAL> <READABLE-BODY>`. The receiver
still accepts the original compact form for compatibility. Keeping the
origin and final destination in each part allows an enhanced intermediary to
return selective ACKs and final receipts toward the correct endpoint.

The v1 grammar is documented in [`PROTOCOL_V1.md`](PROTOCOL_V1.md). JS8Call
still receives readable text and applies its own token replacement/varicode
encoding. JS8Mail does not add encryption or an opaque compression layer.

### Multipart emergency behaviour

The destination stores each valid part by message ID and part number. Repeated
parts are ignored. If parts 1 and 3 of a four-part message arrive, the inbox
shows a partial preview containing:

```text
[MISSING PART 2/4]
```

The receiver can send a selective bitmap acknowledgement or resend request.
The sender retransmits only the missing part(s), through the known return path
when possible, and otherwise invokes ordinary route discovery. The incomplete
preview remains visible because partial emergency information is better than a
hidden message.

## Custody and store-and-forward

A custodian is a station that has accepted responsibility for holding a
message until the destination can retrieve it. The local state distinguishes:

`offered → accepted → retrieval_pending → forwarded`

with `failed` as a terminal negative outcome for that custodian.

For a legacy recipient, JS8Mail can submit standard JS8Call store text and
interpret the strongest standard ACK available as custody evidence. It must
not call that end-to-end delivery. For a JS8Mail recipient, a custodian can
preserve the message ID and part metadata, forward parts, and relay the final
`J8M1 DELIVERED` receipt back toward the origin.

When a relay disappears, the intended safe behaviour is to re-plan from the
last proven custodian rather than blindly retransmit from the origin. A
standard unenhanced station cannot provide all of those guarantees, so the UI
will remain at a weaker confidence level.

## Airtime, speed, and retry protection

The daemon estimates airtime conservatively from JS8Call speed and text length.
It persists radio airtime counters across restart and maintains a separate
per-message budget. The rolling radio window and individual-message cap are
deliberately independent.

Speed evidence is recorded per directed link. The adaptive policy is cautious:

- step down after repeated failures;
- step up only after sustained successful evidence and an SNR margin;
- never assume that a remote station decodes the local speed;
- if safe speed control is unavailable in the installed JS8Call build, report
  a recommendation rather than pretending to change it.

Use `--auto-speed` to permit an evidence-backed `MODE.SET_SPEED` request. It
is off by default, and an unsupported or rejected JS8Call command only leaves
the recommendation visible in the audit trail; it does not block ordinary
operation or pretend that the speed changed.

Queries and payload retries have separate exponential backoff. `@ALLCALL
QUERY MSGS` is broad and slow; targeted custodians and recent successful
stations are preferred. The purpose is to leave listening opportunities and
avoid retry storms, relay loops, group ACK storms, and sustained channel use.

## Groups and emergency bulletins

JS8Mail uses existing JS8Call group addressing. The UI keeps a conservative
catalogue including `@EMCOMM`, `@ARES`, `@RACES`, `@RAYNET`, `@NTS`, `@JS8NET`,
`@SKYWARN`, `@WX`, `@AMRRON`, and regional DX groups. Groups observed in RF
traffic are catalogued locally and inactive observed groups are eventually
expired; the built-in defaults remain available.

Received group content appears in the separate group-alert area of the inbox.
It is labelled with its source and path where known. A group alert is not
authenticated merely because it arrived over RF. Automatic group forwarding
is opt-in, and acknowledgements must be designated or suppressed to avoid an
ACK storm.

For emergency use, compose explicitly to the desired group, keep the text
concise and identifiable, and select the appropriate priority. Emergency
priority affects scheduling urgency but does not bypass legal identification,
airtime, or safety limits.

## Band operation

The current MVP is deliberately single-band for automatic decisions. JS8Mail
reads the active JS8Call dial context at startup, refreshes it periodically,
and updates it from frequency events when available. It derives a normalized
band such as `20m`, while retaining the raw dial frequency for provenance.
Small VFO nudges therefore remain in the same routing context.

Observations, temporal links, recently-heard stations, route graphs, and route
planning are filtered to the currently selected band. Evidence recorded on a
different band remains in the database but is not used to trigger an automatic
route on the active band. Unknown-band evidence is likewise excluded from
automatic band-scoped planning. A manual QSY should be allowed to settle before
starting a new route decision.

JS8Mail does not autonomously change frequency yet. Reliable band hopping
requires dwell scheduling, rendezvous signalling, antenna profiles,
return-path handling, and protection against missing a receipt window. Those
controls are not mature enough to enable by default.

An allowed-band profile and optional band hopping remain suitable future work.
Until then, change bands manually in JS8Call and treat evidence from another
band as historical advice, not as an immediately usable route.

## Testing without a second station

You can test the local application without RF using:

```sh
pytest -q
```

The deterministic simulations cover three-hop multipart delivery, one missing
part and selective resend, duplicate-safe reassembly, and group ACK policy.
The API probe is receive-only:

```sh
./start.sh --host 127.0.0.1 --port 2442 --tx-mode observe
```

For real RF testing, two stations are the meaningful minimum: one sender and
one receiver. Sending to your own callsign is useful for checking API handoff,
local JS8Call inbox behaviour, correlation, and loopback observations, but it
does not prove a two-station or multi-hop route. An ordinary JS8Call station
can receive readable legacy text without running JS8Mail; it cannot produce a
JS8Mail end-to-end receipt.

Use short test messages first, a dummy load or suitably low power, and avoid
leaving multiple old test messages active while diagnosing a radio path.

## Troubleshooting

### Connection refused on port 2442

JS8Call is not exposing the configured TCP API, the port differs, or JS8Call
is not running. Enable the API, confirm the port, and pass the matching
`--host`/`--port` values.

### JS8Mail says submitted but no RF is heard

Submission proves only that text reached JS8Call. Check JS8Call's TX state,
audio output meter, PTT/CAT configuration, selected sound card, and dummy-load
test independently. Also check that JS8Call's own compose/TX field is not
occupied by manual text.

### The message remains in Discovery in progress

Inspect the expanded timeline. A small probe or query may have been sent while
there is no current destination response. `blocked` normally means a query
cooldown, TX occupancy, disconnected API, or airtime policy prevented that
one action; it is not a remote negative acknowledgement. The message should
remain queued until its expiry unless the operator cancels it.

### The graph looks crowded or shows unfamiliar stations

Grey links are recent local evidence across the observation window, not all
links attempted by the selected message. Orange links are attempts. Green
links are confirmed evidence. A self-test is not a useful RF path and is
excluded from current graph rendering.

### A standard ACK says `ACK`

That proves an addressed hop or store transaction according to context. It is
not an end-to-end delivery receipt. Only a valid enhanced destination receipt
can produce the strongest `Complete+` confidence.

## Data, privacy, and recovery

Message bodies stay in the local SQLite database and on the RF path. Ordinary
structured logs and route explanations should avoid copying message bodies.
Back up `js8mail.sqlite3` only when the daemon is stopped or SQLite's backup
facilities are used; the database may have WAL sidecar files while running.

The optional online topology service is not required by any local decision.
If enabled in a future build, it should receive only disclosed observation
metadata, never message content, audio, credentials, or private notes. A key
embedded in source would be a public application token, not meaningful
authentication.

## Development and verification

Run the complete local checks:

```sh
./.venv/bin/ruff check src tests
./.venv/bin/mypy src
./.venv/bin/pytest -q
```

The test suite includes protocol parsing, lifecycle transitions, durable
SQLite/migration behaviour, query backoff, route ranking and cycle rejection,
multipart recovery, group handling, API framing, transmit safety, and
radio-free simulations. CI does not require a radio, Internet service, or
running JS8Call instance.

The implementation plan and capability matrix record important boundaries and
unverified JS8Call API details:

- [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md)
- [`API_CAPABILITY_MATRIX.md`](API_CAPABILITY_MATRIX.md)
- [`PROTOCOL_V1.md`](PROTOCOL_V1.md)

## Roadmap boundaries

The local-only foundation is the priority. Future work includes a clearer
route-graph projection that separates global evidence from message-specific
paths, more complete live capability probing, stronger custody forwarding
correlation across real intermediate JS8Mail nodes, optional per-peer speed
control, configured multi-band operation, and the privacy-disclosed shared
topology service. None of these should make the emergency RF path depend on
the Internet.
