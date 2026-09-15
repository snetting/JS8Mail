# JS8Mail enhanced envelope protocol v1

Status: version 1 specification for the early `JS8Mail/0.0.5` implementation.

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

Readable `[JS8Mail/x.y.z]` text is only an identification hint. It can cause
the receiver to schedule a rate-limited CAP response, but it is never treated
as proof of capability. First contact can therefore remain ordinary readable
JS8Call text, while Required mode explicitly sends CAP and waits for the
path-aware response window before falling back.

## Multipart data

Each enhanced body part is:

```text
J8M1 D <MID> <PART>/<TOTAL> <READABLE-BODY>
```

For a routed or stored enhanced transfer, implementations should include the
optional origin-aware form:

```text
J8M1 D <ORIGIN> <DEST> <MID> <PART>/<TOTAL> <READABLE-BODY>
```

Receivers accept both forms for compatibility. The origin-aware form lets an
intermediate JS8Mail custodian address selective acknowledgements and final
receipts back to the original sender instead of treating the last RF hop as
the author.

`MID` is the stable compact message identifier, `PART` starts at 1, and
`TOTAL` is the complete part count. Implementations must bound identifiers,
part counts, field sizes, and total message size. JS8Call performs its normal
token replacement and RF encoding after receiving this readable text.

The `J8M1` marker is intentionally visible. `D`, `CAP`, `PA`, `REQ`, and
`DELIVERED` are the v1 record types. Fields are separated by whitespace,
identifiers are bounded to safe token characters, callsign/path fields are
validated, and implementations reject oversized or unknown records. The final
destination is supplied by the surrounding JS8Call
directed or relay address; the stable `MID` is the correlation key. This keeps
an enhanced record passable through an ordinary JS8Call relay while leaving
the message body readable to an operator who sees it.

Receivers persist parts by `(sender, MID, PART)`, ignore duplicates, and expose
an incomplete preview with explicit missing-part markers. Once all parts are
present they create one inbox item and do not create another item for retries.

## Selective acknowledgements and resend

A part acknowledgement is:

```text
J8M1 PA <MID> <TOTAL> <RECEIVED-BITMAP-HEX>
```

Bit zero represents part 1. A sender derives the missing set and retransmits
only missing parts. A resend request is:

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
the store is marked unconfirmed and automatic retry is deferred; JS8Mail does
not immediately offer the same message to every candidate custodian.

## Compatibility and safety

Malformed, oversized, unknown-version, or unknown-message frames are ignored
safely. All generated frames are validated before queueing. Implementations
must keep message contents local except for the RF transmission itself, and
must not depend on an Internet service. The syntax is compact metadata; the
underlying JS8Call speed, cycle timing, and token replacement determine actual
airtime.
