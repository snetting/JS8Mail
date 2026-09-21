# JS8Mail user and operator guide

JS8Mail is a local, offline-first mail client and delivery scheduler that runs
above an unmodified JS8Call installation. JS8Call remains responsible for the
modem, decoding, audio, CAT/PTT control, JS8 token replacement, and the
ordinary directed-message, relay, heartbeat, hearing, and inbox facilities.
JS8Mail adds a durable mailbox, evidence collection, route selection, custody
tracking, enhanced-peer receipts, multipart recovery, and an operator-facing
web interface.

This document describes the current `0.0.9` outbox, custody-route, scheduler, and stored-message collection safeguard
implementation. It is useful and
radio-capable, and is suitable for supervised on-air use, but it remains early
and experimental. Operators should monitor transmissions and be ready to pause
automation if anything behaves unexpectedly. Extensive testing has not revealed
unexpected transmit loops, but future changes or unforeseen glitches cannot be
guaranteed away. In particular, a route score is evidence-based advice, not a
guarantee that a station is listening now. Always operate within your licence,
local band plan, power limits, and the expectations of other operators.

### Collecting messages held by a JS8Call custodian

When a periodic `QUERY MSGS` reaches a station holding a persistent JS8Call
message, JS8Call may answer with a line such as `MM0ZFG: OH3SPN YES MSG ID
431`. The numeric ID is local to that custodian, so JS8Mail queues the
targeted `MM0ZFG QUERY MSG 431` request. It does not transmit from inside the
receive callback: the normal scheduler waits for a safe JS8Call handoff, leaving
room for the response and any outstanding RX/TX train to settle.

Duplicate announcements are coalesced. A failed or partial collection remains
pending for a bounded number of later opportunities; a complete message clears
the pending item and is placed in the mailbox. Pending retrieval state is
reconstructed from the audit log after a daemon restart, so restarting JS8Mail
does not lose a message that JS8Call has already announced. The UI and audit
timeline distinguish a retrieval that was requested from one that was actually
received.

Retrieval correlation is deliberately conservative: JS8Mail allows only one
`QUERY MSG <id>` response window per custodian. A normal directed message from
that station does not complete unrelated pending IDs. A retrieval is marked
complete only when a matching query was handed to JS8Call and the response
arrived inside its bounded response window; otherwise it is retried or
eventually exhausted according to the normal retrieval budget. This prevents
an old message or an unrelated custodian transmission from permanently
suppression of collection. Historical completion records from older builds that
have no matching query submission are automatically re-armed on restart.

For a genuinely correlated stored collection, the opened inbox view shows the
custodian path followed by the custodian's local JS8Call message ID, for
example `OH3SPN→MM0ZFG · MSG ID 431`. The ID is local to that custodian and is
an analysis aid, not a globally unique JS8Mail message identifier.

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

Python 3.12 or newer is required for the current development build. The
recommended first-start command is:

```sh
./js8mail
```

On first run, `js8mail` creates or repairs `.venv`, asks before installing the
runtime package, checks the JS8Call API, and then starts the daemon in the
background. It uses automatic RF handoff by default and prints a prominent URL
banner. Open <http://127.0.0.1:8765> when the launcher reports that JS8Mail is
running. Daemon output is written to `.js8mail/js8mail.log` beside the launcher.
If another JS8Mail daemon is already running, the launcher reports it and stops
it before starting the new instance. If JS8Call is not running or the API is
disabled, this is a warning with setup guidance; JS8Mail still starts and
retries its connection.

Useful launcher options are:

```sh
./js8mail --check-only       # perform setup/API checks without starting
./js8mail --yes              # do not prompt for local installation
./js8mail --no-install       # fail rather than create/update .venv
./js8mail --tx-mode observe  # no RF submission; useful for initial testing
./js8mail --foreground       # run in this terminal with live daemon output
```

The launcher passes normal daemon options through, including `--host`, `--port`,
`--ui-host`, `--ui-port`, `--auto-speed`, and `--tx-mode`. It installs only the
runtime package; developers who want the test suite should use the explicit
setup in the development section below and install `.[test]`.

### Accessing the UI from another machine

The UI binds to loopback by default. To allow access from a trusted LAN while
keeping the JS8Call control API on the radio computer's loopback interface,
start JS8Mail like this:

```sh
./js8mail --ui-host 0.0.0.0 --ui-port 8765 --host 127.0.0.1 --port 2442
```

Then open `http://<radio-computer-lan-address>:8765` from the other machine,
for example `http://192.168.0.39:8765`. Replace the address with the station's
actual LAN address. Allow TCP/8765 through the host firewall only on the
trusted private network. Do not expose the UI or JS8Call API directly to the
Internet or an untrusted network: the UI can queue, cancel, and control RF
handoff, and the current UI does not provide user authentication.

The `--host` option is the JS8Call API address; normally it should remain
`127.0.0.1`. It is not the address used by browsers to reach the UI.

### Windows desktop bundle

Windows packaging is experimental and currently unvalidated on a physical
Windows workstation. The bundle has not yet been tested end-to-end with
Windows JS8Call, audio devices, or a radio, so it should not yet be treated as
production or emergency-communications software.

The repository also provides a portable Windows executable build. Download the
`JS8Mail-windows-x64` artifact from a successful `Windows bundle` workflow run,
place `JS8Mail.exe` in a writable directory, and double-click it. The bundle
starts with automatic RF handoff, connects to JS8Call at `127.0.0.1:2442`,
serves the UI at `http://127.0.0.1:8765`, and opens that address automatically.
Its local `js8mail.sqlite3` database is kept beside the executable. JS8Call
must still be installed and configured separately; the bundle does not include
JS8Call, audio drivers, or radio control software.

The executable is built on a Windows runner with PyInstaller, so it is a
bundled/frozen Python application rather than a cross-compiled Linux binary.
For a local build, run `builds\\windows\\build.ps1` in PowerShell. The script
creates a temporary build environment and writes
`builds\\windows\\dist\\JS8Mail.exe`. Unsigned local builds may trigger a
normal Windows Defender warning. Use a dedicated writable directory and back
up the SQLite file before replacing the executable.

For live automatic handoff, simply omit `--tx-mode` (automatic is the
default):

```sh
./js8mail --host 127.0.0.1 --port 2442 --ui-port 8765
```

For a controlled speed experiment, add `--auto-speed`. Without that flag the
daemon records speed evidence and displays recommendations but leaves the
JS8Call mode unchanged:

```sh
./js8mail --host 127.0.0.1 --port 2442 --ui-port 8765 --auto-speed
```

The current command-line modes are:

| Mode | Behaviour |
| --- | --- |
| `observe` | Capture events and build evidence, but do not submit RF text. |
| `approve` | Reserved safety mode for an approval-gated workflow. |
| `automatic` | Submit eligible JS8Mail frames to JS8Call after the safety checks. |

The web UI is local-only by default. It is the operator control surface for
queueing, inspecting, retrying, cancelling, removing completed/failed local
mail, selecting the system JS8M mode, pausing RF handoff, and opening the
message/path graph. The UI does not bypass the daemon's validation, one-at-a-
time transmit arbiter, route evidence, or airtime budgets.

The current UI is intentionally functional rather than visually elaborate. It
contains an inbox, compose form, recently heard stations, emergency/group
catalogue, outbox timeline, graph view, and recent observations. Queueing a
message scrolls the page to the outbox. Expanding an outbox item pauses full
mailbox redraws so the detail view does not collapse while reading it.

Inbox items are stored locally and highlighted with a pale blue row until
opened. Opening an item records the read state in SQLite, so the highlight is
not restored after a restart. If a partial message later gains new parts, it
becomes unread again so the changed content is visible.

The inbox is an end-user mailbox, not a protocol trace. JS8Mail removes the
optional leading or trailing `[JS8MAIL/x.y.z]` identification marker from
displayed standard mail and keeps only the readable payload. Capability advertisements, multipart
headers, selective acknowledgements, resend requests, delivery receipts, and
malformed activity copies are retained in Recent Observations and the audit
log, but are not shown as separate inbox messages.

The protocol badge describes how that message was delivered. A `Standard`
message may also show a small green dot after the sender callsign when the
sender currently has valid JS8Mail capability evidence. The dot is independent
of the message protocol; hovering it shows `JS8Mail capable`. It is a
forward-looking hint for future Opportunistic messages, not a claim that the
displayed message used JS8M.

Subjects are carried in enhanced JS8Mail v1 data on the first part. For
Standard JS8Call delivery they are rendered as readable `subject: body` text,
so important subject information is not silently lost.

Enhanced wire overhead is deliberately small but not zero. A one-part data
frame adds the visible `J8M1 D`, message ID, part count, and separators around
the readable body. If a subject is present, the first part also adds a compact
`{S:...}|` field; spaces and other characters may expand as percent-encoded
text. CAP, PA, REQ, and DELIVERED records are additional control frames and
therefore consume separate JS8Call transmission opportunities. Routed or
stored messages can add origin/destination metadata and relay prefixes.

The underlying JS8Call modem still performs its normal token replacement and
RF encoding, so character count alone is not an exact airtime calculation.
Longer metadata can push a message over a JS8Call frame boundary and create an
additional part or transmission cycle. JS8Mail estimates airtime using the
active JS8Call speed, records actual TX timing when JS8Call reports it, and
leaves receive gaps between trains. This is why a short Standard message can
be faster than an Enhanced message with CAP/PA/receipt exchange, while
Enhanced mode provides stronger delivery and recovery evidence.

The header also shows the current JS8Call connection, station callsign, active
band and dial frequency, RF pause state, and compact RX/DCD/TX/ERR/JS8
indicators. RX is the normal receive state; DCD is lit when JS8Call reports a
decode event; TX is lit during an observed transmit; ERR indicates a control
or connection problem; JS8 briefly indicates JS8Mail-to-JS8Call activity.
The band is read from JS8Call and is part of the active routing context. A
frequency value is retained for audit/provenance, but small VFO offsets do not
create a separate route graph.

## JS8Call configuration

JS8Mail does not blindly edit JS8Call configuration files. Configure and test
JS8Call itself first, then start JS8Mail.

### Enable the local TCP/JSON API

In JS8Call, open the networking/API settings and enable the documented TCP
server/API. The usual local endpoint is:

- address: `127.0.0.1` (loopback);
- port: `2442`.

Use the exact port shown by your installed JS8Call build; pass it to
`js8mail --port`. Do not expose the API to an untrusted network interface.
The API is a control surface for the radio, not an Internet service.

The official API references are:

- [JS8Call API documentation](https://js8call.com/JS8Call-improved/d7/d15/md_docs_2API.html)
- [JS8Call upstream project](https://github.com/js8call/js8call)
- [JS8Call-improved source](https://github.com/JS8Call-improved/JS8Call-improved)

If the API is disabled, the probe will fail with `ConnectionRefusedError`.
That means JS8Mail is not listening on the port; it does not indicate an RF
problem.

### Unattended operation and JS8Call idle detection

JS8Call has an operator-idle safety timer. Depending on the build, the setting
may be shown as **My Station Idle Time**, **Idle Timeout**, or represented in
the configuration as `TxIdleWatchdog`. It is based on keyboard/mouse activity,
not on whether the TCP API socket is still connected. After the timeout,
JS8Call can disable automatic transmissions such as heartbeats and auto-replies
until operator activity is detected.

For an unattended or overnight JS8Mail station, first confirm that unattended
operation is permitted by your local licence conditions and band plan. Always
observe those conditions. If permitted, disable this JS8Call idle timer (or use
the build's equivalent of disabled) and disable operating-system suspend on AC
power. Confirm the setting after upgrading JS8Call, since the label and
defaults may differ between builds.

JS8Mail's API status polling and reconnect logic can detect a closed or
unresponsive API connection, but they do not reset JS8Call's keyboard/mouse
idle timer. JS8Mail deliberately does not generate dummy RF traffic merely to
keep JS8Call awake: that would consume airtime and could interfere with real
mail.

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
message remains queued. The expanded outbox timeline explains why a query was
blocked, for example `query cooldown active; next attempt in 120s`, another
`@ALLCALL` query awaiting its response window, `JS8Call API disconnected`, or
an RF handoff blocked because JS8Call is receiving/transmitting or a local
airtime policy is full. This prevents a fleet of queued messages from turning
into a broadcast beacon while making the reason visible to the operator.

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

For a multi-hop directed message, JS8Mail uses JS8Call's forwarded-command
syntax, with a separator before the final `MSG` command:

```text
IZ1KJG>MM0ZFG>MSG readable message
```

This is different from a direct message (`MM0ZFG MSG ...`) and from a free
text relay (`IZ1KJG>MM0ZFG>readable message`). The extra separator is required
for JS8Call to recognize the final directed command and provide its normal
relay/ACK behavior.

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

An unsuccessful direct attempt does not permanently disqualify the direct
route. The attempted path and its result are persisted, the path is cooled
down, and the next-ranked viable path may be tried. On a later retry the
planner scores all currently viable paths again, including previously failed
ones, so a route can recover when propagation changes. A newly observed direct
reply or a remote `YES` report wakes matching queued messages immediately; it
does not wait for the old retry timer.

### JS8M sending mode

The **System sending mode** selector sets the default for new directed
messages, with an optional per-message override. It is deliberately separate
from the daemon's RF handoff mode:

- **Standard** sends ordinary JS8Call-readable text, emits no JS8Mail marker,
  and does not wait for a JS8Mail capability exchange;
- **Opportunistic** (the default) uses enhanced framing for peers whose valid
  `CAP` advertisement or JS8Mail frame has already been observed. For an
  unknown peer it sends ordinary readable mail immediately, adding the
  `[JS8MAIL/x.y.z]` marker on first contact. A complete direct recipient that
  runs JS8Mail queues one delayed, rate-limited CAP response after the normal
  receive/ACK opportunity. The sender then records the capability and returns
  its own CAP when needed, so later messages can use enhanced framing. If the
  CAP exchange is lost, the original ordinary delivery remains valid;
- **Required** sends `J8M1 CAP` first and waits for a capability response
  before using enhanced framing; if no response arrives within the calculated
  path-aware window, it falls back to ordinary delivery according to policy.

Group and bulletin messages are always Standard. Every receiver still parses
valid JS8Mail frames and can answer capability, part, resend, and delivery
receipts regardless of its outbound default. A `CAP` advertisement is not an
ACK: it advertises features, while a valid JS8Mail data/ACK/receipt frame is
passive evidence of the specific feature it demonstrates. A normal JS8Call
`ACK` remains hop evidence.

When a JS8Mail receiver gets a complete ordinary direct message containing the
readable version marker, it records the marker as provisional software
evidence and queues one delayed, rate-limited CAP response. The response is
submitted only after the receive/ACK opportunity and a quiet gap, so it cannot
pre-empt the incoming mail. Explicit `J8M1 CAP` exchanges remain bidirectional.
Standard mode never adds a CAP before or after an ordinary message. Required
mode sends one CAP before the first directed payload when no valid record
exists, then waits for the path-aware response window before falling back. A
client may also return a CAP toward an original sender after collecting stored
JS8Mail. The daemon does not emit unsolicited periodic CAP beacons. These
rules avoid filling quiet periods or colliding with another station's next TX
train.

Any JS8Mail listener that decodes a complete CAP—whether the exchange is
addressed to it or merely overheard—stores the advertising callsign, protocol
version, and feature list. A partial or ambiguous CAP is retained in Recent
observations but does not create a full capability record. A valid enhanced
data, part-ACK, or delivery-receipt frame can add only the feature it proves;
it must not be treated as proof of every JS8Mail feature. Thus other listeners
can identify and flag observed JS8Mail endpoints without replying to traffic
that was not addressed to them.
The marker does not change the current message into an enhanced message and
does not apply to group broadcasts. It identifies JS8Mail software but does
not claim `E2E`, `MP`, `PA`, or `RR`; only a CAP or a frame demonstrating a
specific feature creates confirmed capability evidence. If no CAP or valid
JS8Mail frame is ever observed, Opportunistic mode remains on ordinary
JS8Call delivery.

Capability records are retained for seven days from the latest valid evidence.
Fresh CAP, enhanced data, part-ACK, or delivery-receipt evidence refreshes the
expiry. After expiry, Opportunistic mode treats the peer as unknown and begins
with ordinary marked delivery again; Standard mode remains a one-message
opt-out and does not restart discovery.

### 9. Submit safely to JS8Call

Before submission JS8Mail validates callsigns, sizes, framing, and airtime.
The transmit path also checks that JS8Call is connected and that its current
transmit text is not occupied by the operator. It never overwrites manual
text. A single lock serializes automated submissions, and a polite gap is
left between JS8Mail handoffs so the daemon can hear replies and does not
stack several automatic requests into the same JS8Call opportunity.

The submission event means “JS8Call accepted text for its next opportunity.”
It does not prove that the frame was decoded by anybody.

The transmit arbiter does not immediately hand the next queued request to
JS8Call. It waits for an observed TX→RX transition when available, then keeps
a 60-second receive hold for ACKs, query answers, or other delayed evidence.
When a TX-state event is unavailable, it uses the conservative estimated
airtime plus the same receive hold before admitting another automated
request. This protects the response window after restarting or unpausing with
several queued messages. A new message remains queued while an existing
transaction has priority.

An unclassified RX event also starts a bounded two-cycle observation hold
(about 30 seconds at the normal 15-second JS8Call cycle). Unrelated traffic
does not extend that hold forever on a busy band. Directed/incoming mail has a
separate guard and can extend protection while its frames are being assembled,
so automated TX does not interrupt a message addressed to this station. After
the generic two-cycle window, queued work may transmit even if other stations
remain active; this preserves a fair opportunity for delayed replies without
starving the mailbox.

## Route evidence and delivery states

The outbox deliberately distinguishes these states:

| UI meaning | What it proves |
| --- | --- |
| Submitted to JS8Call | Text reached the local JS8Call API. |
| Frames observed | JS8Call produced a TX frame; remote decoding is unproven. |
| Standard | An addressed station acknowledged ordinary JS8Call delivery; JS8Mail end-to-end delivery is unproven. |
| Stored at custodian | A custodian acknowledged a store operation; recipient retrieval is still pending. This stops automatic re-offering; the operator can retain it or start a fresh retry deliberately. |
| Acknowledged | JS8Call accepted an enhanced message, but no JS8Mail end-to-end receipt arrived after the bounded retry policy. Automatic retries are held; the operator may retry manually. |
| Delivered / Complete | An ordinary/known delivery conclusion supported by local evidence. |
| Complete+ | A JS8Mail destination sent an end-to-end delivery receipt. |
| Read | Only available for an explicitly enabled read receipt. |
| Discovery in progress | No stronger conclusion is currently proven. |

The **Recent observations** panel is deliberately compact. It shows the latest
radio/protocol evidence after filtering high-volume PTT and TX-frame plumbing,
so partially received activity streams, `J8M1 CAP` negotiations, enhanced data,
receipts, and ordinary decoded traffic remain visible. The full durable
outbox/audit timeline remains the authoritative place for every attempt and
state transition.

Standard JS8Call mail is reassembled from JS8Call activity fragments when a
long message is exposed that way. A temporary Partial inbox item is expected
while continuation frames are arriving. JS8Mail only upgrades it to Complete
after the final activity/direct-message event is seen; a missing final flag is
reported as partial rather than guessed complete. When JS8Call emits both an
activity copy and a reconstructed final directed event, the copy is ignored
only to avoid duplication—the reconstructed event is still processed as the
authoritative inbox message.

The graph view uses these colours:

- grey: observed RF evidence;
- orange: an attempted message edge;
- green: a confirmed edge/receipt.

The graph is an evidence view, not a promise that every grey edge is an
available relay. A future UI improvement should separate “global observations”
from “paths actually attempted for this message” even more explicitly.

The **Live RF Activity** graph is built from both decoded RX traffic and
submitted/settled local TX transactions. JS8Call's TX tone events do not
contain the original text, so the local direction is recovered from JS8Mail's
durable transaction and link records. Green means both directions have fresh
evidence (currently a ten-minute active window); orange means recent one-way
activity; grey means one-way evidence that has aged. Thick edges indicate
JS8Mail-related evidence. The local station is intentionally excluded from
the Recently heard list.

For enhanced multipart traffic, a plain ACK is intentionally weaker even when
it comes from the final callsign: it confirms a JS8Call hop, not complete
reassembly by the JS8Mail client. The sender waits for `J8M1 DELIVERED`. If the
destination reports a missing-part bitmap, JS8Mail retransmits only those
parts, using the recorded reverse path when available. If a final receipt
contains a known accepted custodian, the outbox also records that custodian as
forwarded. Standard JS8Call relay/store remains the compatibility transport;
an intermediate station is not assumed to run JS8Mail merely because it
carries an opaque `J8M1` frame.

Enhanced parts are currently sent one at a time: after each part, the sender
leaves an RX opportunity for a cumulative `J8M1 PA` bitmap. One reply can
acknowledge several parts if they arrived before the reply was transmitted;
otherwise one reply per new part is normal. The bitmap is persisted, so a
restart resumes from the next missing part rather than immediately replaying
the entire body. JS8Mail starts delivery-response timers only after the final
RF frame of a JS8Call transmit train has settled, not when it merely submits
text to the TCP API or sees a short inter-frame PTT gap. These protections
still require two-station on-air validation before a wider release.

For a standard stored message, a response such as `YES MSG 426` is correlated
with the exact JS8Call message identifier when available. If the returned
content is truncated or has missing sections, JS8Mail creates or updates a
visible amber Partial inbox item and issues a bounded `QUERY MSG 426` retry.
When the complete response arrives it reconciles the same item to Complete;
it does not create a duplicate. This recovery is necessarily less expressive
than JS8M part acknowledgements because a legacy station does not know the
JS8Mail part bitmap.

### Legacy ACK timing and relay semantics

JS8Call's ordinary `ACK` is expected for both a direct `MSG` and a successful
`MSG TO:` store offer. JS8Call also returns a final-destination ACK through a
reverse relay path for a relayed message. JS8Mail waits for the actual
TX-to-RX transition before starting the response deadline and scales that
deadline with relay hop count. A late ACK is still correlated with the durable
transmission that produced it, even if the message has returned to discovery.

If a store ACK is received, the outbox becomes **Stored · ACK**: this proves
custodian acceptance only, not recipient collection. If the deadline expires
without an ACK, the result is explicitly storage-unconfirmed and the message
waits before considering another custodian; it is never immediately offered to
every candidate. A relayed ACK is parsed using its explicit final-destination
marker (`*DE*`), so an ACK from a carrying relay cannot be mistaken for
end-to-end delivery. A later destination or JS8Mail receipt can still
reconcile the original message.

## JS8Mail enhanced peers

For an unknown station, the wire choice follows the selected sending mode.
Standard and Opportunistic begin with ordinary readable JS8Call text;
Required begins with a separate capability advertisement and falls back to
ordinary delivery if the response window expires:

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

### Receive-frame reassembly and interleaving

JS8Call normally exposes a complete directed message through `RX.DIRECTED`.
Some builds also expose the short pieces through `RX.ACTIVITY` before, or
instead of, that complete event. JS8Mail records those observations and uses
the documented `BITS` flags when it has them: bit 0 (`BITS & 1`) marks the
first frame and bit 1 (`BITS & 2`) marks the last frame. Other bits are
ignored, so values such as 5 and 6 still mean first and last respectively.

The assembler keeps more than one pending stream and matches continuations by
the available band, dial, offset, speed, and timing context. A new
callsign-prefixed activity line, heartbeat, or unrelated directed exchange
does not clear an existing stream. If two streams could accept the same
continuation, or an observed timestamp arrives out of order, JS8Mail marks
the result ambiguous and leaves it partial instead of silently joining the
wrong text. Standard JS8Call activity has no universal application sequence
number, so the UI cannot honestly name an exact missing standard part; it
shows the received prefix/body as Partial until an authoritative complete
`RX.DIRECTED` event or a safe final activity frame arrives.

For older builds that do not provide `BITS`, a trailing JS8Call continuation
marker is used only as a lower-confidence legacy completion hint. It is not
treated as proof equivalent to `RX.DIRECTED`. Enhanced `J8M1` messages remain
part-numbered and use their selective part ACK/resend mechanism, which is the
reliable way to identify a specific missing part.

## Custody and store-and-forward

A custodian is a station that has accepted responsibility for holding a
message until the destination can retrieve it. The local state distinguishes:

`offered → accepted → retrieval_pending → forwarded`

with `failed` as a terminal negative outcome for that custodian.

For a legacy or unknown custodian, JS8Mail submits a readable standard
`MSG TO:` store offer, even when the message's default mode is Opportunistic.
This is deliberate: a custodian with no current JS8M capability evidence must
not be given an opaque `J8M1 D` body that it cannot interpret or preserve.
When the custodian has an unexpired JS8M capability record, JS8Mail may use
the compact `J8M1 D` store envelope (and selective multipart handling when
`MP` is advertised). Capability evidence is checked per custodian, including
after a restart, and stale evidence falls back to readable standard storage.
JS8Mail can interpret the custodian's standard ACK as custody evidence, but it
must not call that end-to-end delivery. For a direct ordinary `MSG`, an ACK from the final
destination is stronger: it means JS8Call accepted the complete message into
that destination's JS8Call inbox, and JS8Mail may show ordinary **Complete**.
For a JS8Mail recipient, a custodian can
preserve the message ID and part metadata, forward parts, and relay the final
`J8M1 DELIVERED` receipt back toward the origin.

When a JS8Mail client collects a stored message, it also returns a directed,
rate-limited capability declaration toward the original sender. It first uses
the recorded reverse custody path when that path is available; otherwise it
makes a direct capability/reachability attempt toward the original sender and
allows normal route evidence to improve later attempts. The Outbox shows this
as **JS8Mail discovery · delivery confirmation** under **Automatic delivery
confirmations**. This is an automatic protocol control exchange, not a
user-authored message and not, by itself, proof that the original sender has
received the final message. A final enhanced delivery receipt remains the
strongest confirmation.

When a relay disappears, the intended safe behaviour is to re-plan from the
last proven custodian rather than blindly retransmit from the origin. A
standard unenhanced station cannot provide all of those guarantees, so the UI
will remain at a weaker confidence level.

## Airtime, speed, and retry protection

The daemon estimates airtime conservatively from JS8Call speed and text length.
It persists the current radio-window usage across restart and maintains a
separate per-message budget. The rolling radio window and individual-message
cap are deliberately independent. The station-wide budget has no cumulative
lifetime cap: an old `message_used_ms` value from earlier builds is retained
only as legacy diagnostic data and cannot permanently lock the station after a
restart or across rolling windows.

Speed evidence is recorded per directed link. The adaptive policy is cautious:

- step down after repeated failures;
- step up only after sustained successful evidence and an SNR margin;
- never assume that a remote station decodes the local speed;
- if safe speed control is unavailable in the installed JS8Call build, report
  a recommendation rather than pretending to change it.

Use `--auto-speed` to permit an evidence-backed `MODE.SET_SPEED` request. It
is off by default, and an unsupported or rejected JS8Call command only leaves
the recommendation visible in the audit trail; it does not block ordinary
operation or pretend that the speed changed. Keep this opt-in for the first
community test release: the current policy does not yet use a verified
receiver-reported SNR to select the first payload speed, and asymmetric
TX/RX speed timing needs two-station validation. The initial reachability
probe should use normal speed so an unknown peer is not assumed to decode a
faster mode.

Queries and payload retries have separate exponential backoff. `@ALLCALL
QUERY MSGS` is broad and slow; targeted custodians and recent successful
stations are preferred. The purpose is to leave listening opportunities and
avoid retry storms, relay loops, group ACK storms, and sustained channel use.

The current default safety budgets are explicit. A single message may use at
most 10 minutes in its rolling per-message burst window (the accounting
cadence is 15 minutes) and at most 60 minutes cumulatively over its lifetime.
The station-wide rolling budget is independent and is persisted in SQLite
across daemon restarts. A rolling-window block defers transmission until the
actual next eligible window boundary, rather than blindly waiting a new full
15 minutes. Reaching the message lifetime ceiling marks that message
Failed rather than retrying forever. These are local policy blocks, not
evidence that the radio is busy, and they do not prevent other messages from
being considered when their own budgets permit.

## Groups and emergency alerts

JS8Mail uses existing JS8Call group addressing. The UI keeps a conservative
catalogue including `@JS8MAIL` (discussion and updates), `@EMCOMM`, `@ARES`, `@RACES`, `@RAYNET`, `@NTS`, `@JS8NET`,
`@SKYWARN`, `@WX`, `@AMRRON`, and regional DX groups. Groups observed in RF
traffic are catalogued locally and inactive observed groups are eventually
expired; the built-in defaults remain available.

`@JS8MAIL` is subscribed by default so a fresh installation can receive
discussion and update traffic. This is only a local default: the operator can
unsubscribe it at any time, and JS8Mail never forwards group traffic
automatically merely because the group is subscribed.

Received group content appears in the separate group-alert area of the inbox.
It is labelled with its source and path where known. A group alert is not
authenticated merely because it arrived over RF. Automatic group forwarding
is opt-in, and acknowledgements must be designated or suppressed to avoid an
ACK storm.

Outgoing group broadcasts are ordinary JS8Call group messages. JS8Mail does
not add a protocol marker to them: a marker is not needed for group delivery
or capability discovery, and omitting it preserves airtime and avoids making a
group post look like a directed capability invitation. Group broadcasts are
reported as submitted with no ACK expected; they are not treated as end-to-end
delivery confirmations.

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
Until then, change bands manually in JS8Call and allow the active-band view to
settle before testing a new route. Evidence from another band remains useful
historical data, but cannot trigger an automatic route on the current band.

## Testing without a second station

You can test the local application without RF using:

```sh
pytest -q
```

The deterministic simulations cover three-hop multipart delivery, one missing
part and selective resend, duplicate-safe reassembly, group ACK policy, and
activity-frame cases including out-of-order and interleaved decodes.
The API probe is receive-only:

```sh
./js8mail --host 127.0.0.1 --port 2442 --tx-mode observe
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

### JS8Call reports that the station has been idle

This is normally the JS8Call operator-idle timer, not a JS8Mail mailbox or
route failure. Disable the idle timer for unattended operation, check that the
computer has not suspended, and confirm that JS8Call still reports PTT/audio
and heartbeat activity before investigating RF routing. JS8Mail cannot safely
reset this timer through the documented API.

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

The meaning depends on the transaction. A direct final-destination ACK proves
that JS8Call accepted the complete ordinary message into the destination's
inbox and can produce ordinary **Complete**. A relay final ACK proves the same
thing when it returns through the reverse path. A custodian ACK proves only
that the `MSG TO:` store operation was accepted for later collection, so the
outbox shows **Stored · ACK**, not Complete. A plain ACK for an enhanced
multipart transfer remains hop evidence; only a valid `J8M1 DELIVERED` receipt
can produce **Complete+**.

To prevent an enhanced message from retransmitting indefinitely when the
destination's JS8Call accepts it but its JS8Mail receipt is lost, JS8Mail
allows one retry and then holds automatic retries after two distinct final-peer
ordinary ACK opportunities. The outbox shows **Acknowledged · JS8Call accepted
· JS8Mail receipt unconfirmed**. The operator can use **Retry now** if a later
receipt is still expected; the original message ID is retained.

### Legacy store offers and missing ACKs

The JS8Call v3.0.3 implementation accepts `C MSG TO:DEST text` into its local
store and then queues `C ACK` (or the complete reverse relay path followed by
`ACK`) through its automatic-reply path. This is visible in the [JS8Call
v3.0.3 command handler](https://github.com/JS8Call-improved/JS8Call-improved/blob/v3.0.3/JS8_Mainwindow/processCommandActivity.cpp).
That reply can still be suppressed by
JS8Call's automatic-reply setting, operator-idle protection, an occupied text
buffer, or radio/API timing. Therefore, a missing ACK is ambiguous: it does
not prove that the custodian rejected or failed to store the message.

JS8Mail consequently never upgrades a timeout to **Stored**. It shows the
attempt as **custody unconfirmed** and records the uncertainty. To avoid
duplicate store spam, one ambiguous timeout permits one further automatic
offer after a short two-minute cooldown; alternate routes and custodians are
considered first. Two unanswered offers quarantine that custodian for 24 hours,
with longer exponential quarantine after repeated failures. This is scoped to
that custodian and never blocks another path. A later custody ACK or JS8Mail
delivery receipt can still reconcile a late result because the original
transmission is kept in the durable transaction history. **Retry now** remains
an explicit operator override.

Relay paths have a matching protection: two completed relay transactions that
time out quarantine the exact path for 24 hours, escalating after repeated
failures. Local/API errors, radio-busy deferrals, airtime blocks, and
incomplete transmissions do not penalize the remote relay, because they do not
prove that the relay refused anything. The relay may still be used for other
destinations or after the quarantine expires.

## Data, privacy, and recovery

Message bodies stay in the local SQLite database and on the RF path. Ordinary
structured logs and route explanations should avoid copying message bodies.
Back up `js8mail.sqlite3` only when the daemon is stopped or SQLite's backup
facilities are used; the database may have WAL sidecar files while running.

The optional online topology service is not required by any local decision.
The client-side upload/download integration is not enabled in this release.
If enabled in a future build, it should receive only disclosed observation
metadata, never message content, audio, credentials, or private notes. A key
embedded in source would be a public application token, not meaningful
authentication; it is only a low-friction abuse filter and must not be treated
as a secret.

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

## Current limitations and roadmap boundaries

The local-only foundation is the priority, but a few boundaries remain
important. The current daemon does not autonomously change bands, and it does
not yet import/reconcile every message already sitting only in JS8Call's local
inbox if the corresponding live API event was missed. Standard JS8Call
custodians provide compatibility store-and-forward; only a JS8Mail-aware
custodian can preserve part IDs and provide enhanced selective recovery.
The shared topology service remains an optional future component, as do richer
prediction, configured multi-band dwell scheduling, and broader cross-station
custody reconciliation. None of these should make the emergency RF path
depend on the Internet.
