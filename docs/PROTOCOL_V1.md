# JS8Mail enhanced envelope protocol v1

Status: version 1 specification for the early `JS8Mail/0.0.8b` implementation.

This is the normative v1 wire specification, not an assertion that every
JS8Call build exposes every transport feature. The adapter must capability
probe the local API, and a station must never infer that a remote station is a
JS8Mail peer merely because it can decode ordinary JS8Call traffic.

This protocol adds correlation and recovery metadata to JS8Call messages. It
does not replace JS8Call's modem, compression/token replacement, addressing,
identification, or legal operating rules. Message body text remains readable.
JS8Mail does not add encryption or application compression in v1.

## Capability discovery

An implementation advertises support with:

```text
J8M1 CAP 1 E2E,MP,PA
```

Features are comma-separated and may include `E2E` (delivery receipts), `MP`
(multipart), `PA` (part acknowledgements), and `RR` (opt-in read receipts).
Feature names are case-insensitive on input and are canonicalised to uppercase
on output. Unknown or malformed features invalidate the advertisement.

`J8M1 CAP` is a bidirectional capability advertisement, not a delivery ACK.
When a client receives a valid CAP, it records the peer's capabilities and
returns one directed CAP advertisement when its response throttle permits.
The response is retried if JS8Call is busy or the API handoff fails; a failed
response must not consume the peer's throttle interval. A peer is enhanced
only when a valid CAP advertisement has been received and remains within its
local expiry period (currently seven days).

Readable `[JS8Mail/x.y.z]` text is only an identification hint. It is recorded
as passive evidence, but it does not cause an unsolicited CAP response: doing
so can collide with the sender's next TX train, particularly when a standard
message is followed by enhanced traffic. It is never treated as proof of
capability. Explicit `J8M1 CAP` remains the bidirectional handshake, while
CAP observations provide passive discovery. First contact can
therefore remain ordinary readable JS8Call text, while Required mode explicitly
sends CAP and waits for the path-aware response window before falling back.

## Multipart data

Each enhanced body part is:

```text
J8M1 D <MID> <PART>/<TOTAL> <READABLE-BODY>
```

The first part may carry a bounded subject before the user payload:

```text
J8M1 D <MID> 1/<TOTAL> {S:subject%20text}| readable body
```

`S` is URL-escaped UTF-8 subject metadata, limited to 120 characters. It is
removed before the body is shown in the inbox. Standard JS8Call messages use
the readable form `subject: body`; this makes the subject visible to stations
that do not run JS8Mail, without requiring them to understand the envelope.

For a routed or stored enhanced transfer, implementations should include the
optional origin-aware form:

```text
J8M1 D <ORIGIN> <DEST> <MID> <PART>/<TOTAL> <READABLE-BODY>
```

Receivers accept both forms for compatibility. Direct and ordinary relay
delivery SHOULD use the compact form because JS8Call supplies the immediate
source and destination. The origin-aware form is required for a JS8Mail
store/custody offer, where the standard mailbox does not guarantee that the
original author remains explicit in retrieved text. It lets an
intermediate JS8Mail custodian address selective acknowledgements and final
receipts back to the original sender instead of treating the last RF hop as
the author.

`MID` is the stable compact message identifier, `PART` starts at 1, and
`TOTAL` is the complete part count. Implementations must bound identifiers,
part counts, field sizes, and total message size. JS8Call performs its normal
token replacement and RF encoding after receiving this readable text.

### Overhead and airtime

The enhanced envelope adds the `J8M1` record marker, record type, stable
message ID, part numbering, separators, and—on the first part—a compact
percent-encoded subject field when one exists. Routed and stored forms may
also add origin, destination, and relay-path fields. These bytes are visible
to an operator who inspects the JS8Call text, but JS8Call still applies its
normal token replacement and modem encoding afterward; JS8Mail does not
replace or duplicate that compression layer.

Control records (`CAP`, `PA`, `REQ`, and `DELIVERED`) are separate directed
frames and consume their own JS8Call opportunities. Consequently, protocol
overhead is both text overhead and, at frame boundaries, possible additional
airtime. Exact duration depends on the installed JS8Call speed, token
replacement, frame length, relay prefixes, and RF scheduling. Implementations
should estimate before queueing, measure observed TX duration where available,
and never assume a fixed seconds-per-character conversion.

The `J8M1` marker is intentionally visible. `D`, `CAP`, `PA`, `REQ`, and
`DELIVERED` are the v1 record types. Fields are separated by whitespace,
identifiers are bounded to safe token characters, callsign/path fields are
validated, and implementations reject oversized or unknown records. The final
destination is supplied by the surrounding JS8Call
directed or relay address; the stable `MID` is the correlation key. This keeps
an enhanced record passable through an ordinary JS8Call relay while leaving
the message body readable to an operator who sees it.

### Activity-frame reconstruction

`RX.DIRECTED` is authoritative when available. An adapter that receives only
`RX.ACTIVITY` may reconstruct a local directed message using JS8Call's
`BITS` field: `BITS & 1` is the first frame and `BITS & 2` is the last frame.
The remaining bits are reserved or build-specific and must not be interpreted
as an exact frame number. Because standard activity has no application
sequence number, an implementation must retain a partial preview and refuse
to complete it when continuations are ambiguous or arrive out of order.
Unrelated callsign-prefixed activity may be interleaved without cancelling a
pending stream. A no-`BITS` legacy continuation marker may be accepted only as
lower-confidence compatibility evidence.

Receivers persist parts by `(sender, MID, PART)`, ignore duplicates, and expose
an incomplete preview with explicit missing-part markers. Once all parts are
present they create one inbox item and do not create another item for retries.

CAP/control reconstruction is kept separate from ordinary message
reconstruction. JS8Call may emit a directed capability response as fragments
whose first fragment is only `SOURCE: DESTINATION`, followed by `J8M1 CAP 1`
and the feature list. The adapter reassembles that bounded stream using the
same RF context and first/last flags, tolerates unrelated prefixed activity
between fragments, and accepts it only when the complete capability grammar
is present. A partial CAP is retained as an observation/audit hint but does
not mark the peer enhanced and does not trigger a response. This prevents a
truncated or ambiguous response from being mistaken for proof while ensuring
that a valid multi-frame response is not silently discarded.

## Selective acknowledgements and resend

A part acknowledgement is:

```text
J8M1 PA <MID> <TOTAL> <RECEIVED-BITMAP-HEX>
```

Bit zero represents part 1. A sender derives the missing set and retransmits
only missing parts. `PA` is cumulative: a single bitmap can acknowledge
several parts received since the last reply, and a later bitmap supersedes
an earlier unsent bitmap for the same message. The current sender uses
stop-and-wait for enhanced parts, so a normal exchange usually has one
part acknowledgement per non-final part. Once the body is complete, the
receiver sends `DELIVERED` instead of an otherwise redundant final `PA`.
A `PA` is not end-to-end delivery proof; `DELIVERED` is the separate final
receipt. A resend request is:

Enhanced data is submitted one part at a time. Each logical part may itself
be encoded by JS8Call as several RF frames. `TX.FRAME` is an early frame
preparation event, not a completed-transmission event, so JS8Mail must never
halt an enhanced transfer from that event. JS8Mail waits for the complete
observed PTT train to become quiet, starts the receipt deadline only then, and
protects an RX window for `PA`, `REQ`, or `DELIVERED` before sending the next
logical part. `RIG.TX_HALT` is not part of the normal enhanced delivery path.

```text
J8M1 REQ <MID> <TOTAL> <MISSING-BITMAP-HEX>
```

These are requests/evidence, not proof of successful RF delivery. They are
rate-limited, idempotent, and subject to normal expiry, route, airtime, and
retry policy. Through a custodian or relay, the request follows the recorded
return path when available and otherwise enters ordinary route discovery.

## Delivery receipt

After complete reassembly, the destination may send:

```text
J8M1 DELIVERED <MID> <UTC-MILLISECONDS> <PATH-COMMA-SEPARATED>
```

The receipt proves delivery to the JS8Mail client at the destination, not that
the operator has read it. `PATH` is bounded metadata and may be `?` when the
destination cannot provide a reliable path. Read receipts are separate and
opt-in; they are not part of the automatic delivery receipt.

## Custody and legacy behavior

Custody is an implementation-level state: offered, accepted, retrieval
pending, forwarded, or failed. A standard ACK for a `MSG TO:` operation proves
that the addressed custodian accepted the store operation, not that the
recipient collected the message. For a direct ordinary `MSG`, an ACK from the
final destination proves that JS8Call accepted the complete message into its
inbox. For a relayed ordinary message, the final ACK may return through the
reverse relay path. A non-enhanced recipient receives ordinary
`DEST MSG readable text` and JS8Mail reports no stronger result than the
evidence supports.

Relays and custodians must preserve the `MID` and part metadata when forwarding
enhanced messages. They must deduplicate repeated data and receipts, respect
bounded hop/TTL policy, and never claim authentication beyond JS8Call's normal
radio evidence.

The JS8Call transport form for an enhanced directed message is:

```text
RELAY1>DEST>MSG J8M1 D ...
```

The `>` before `MSG` is significant. Capability advertisements and other
free-text control hints use the corresponding free-text relay form when they
are forwarded (`RELAY1>DEST>J8M1 CAP ...`).

An ordinary JS8Call `DEST MSG TO:CUSTODIAN body` store transaction is not an
enhanced custody transfer: its ACK proves only that JS8Call accepted the store
operation. The current implementation deliberately relies on JS8Call's
standard relay/store machinery to carry opaque `J8M1` text; it does not pretend
that every intermediate station is a JS8Mail application relay. An enhanced
custodian can retain origin-aware parts and return selective acknowledgements
when its API/path context permits. The origin can claim `Complete+` only after
a receipt that identifies the original `MID` and final destination. A forwarded
receipt is evidence of delivery, not cryptographic authentication.

When a final receipt names a known accepted custodian in its path, the sender
reconciles that custodian as `forwarded` even though the receipt was transmitted
by the final destination. A plain ACK for an enhanced part remains hop evidence;
it cannot promote the message to ordinary `Complete` or enhanced `Complete+`.

The origin stores a durable transmission transaction for every direct,
multipart, relay, and store handoff. Its response deadline begins at the
observed TX-to-RX transition when available, and relay deadlines include the
number of reverse-path hops. A late ACK can therefore be reconciled after a
message has returned to route discovery. If a custodian ACK deadline expires,
the store is marked unconfirmed and automatic retry is deferred. Because the
underlying protocol does not expose a portable end-to-end storage receipt,
JS8Mail records each submitted legacy offer durably and bounds automatic
re-offers to two per custodian, with a one-hour cooldown before the second
offer. It may try another eligible custodian (up to the existing three
distinct-custodian limit), but it does not repeatedly offer the same message to
the same station indefinitely. An operator's explicit Retry now is an
intentional override. A successful standard custodian ACK changes the result
to `Stored · ACK`; it still does not prove recipient retrieval.

## Compatibility and safety

Malformed, oversized, unknown-version, or unknown-message frames are ignored
safely. All generated frames are validated before queueing. Implementations
must keep message contents local except for the RF transmission itself, and
must not depend on an Internet service. The syntax is compact metadata; the
underlying JS8Call speed, cycle timing, and token replacement determine actual
airtime.
