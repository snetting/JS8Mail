# JS8Mail enhanced envelope protocol v1

Status: version 1 specification for the early `JS8Mail/0.0.1` implementation.

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
Unknown features are ignored. A peer is enhanced only when a valid capability
advertisement has been received and remains within its local expiry period
(currently seven days). First contact therefore uses ordinary readable
JS8Call text plus, optionally, a separate capability advertisement.

## Multipart data

Each enhanced body part is:

```text
J8M1 D <MID> <PART>/<TOTAL> <READABLE-BODY>
```

`MID` is the stable compact message identifier, `PART` starts at 1, and
`TOTAL` is the complete part count. Implementations must bound identifiers,
part counts, field sizes, and total message size. JS8Call performs its normal
token replacement and RF encoding after receiving this readable text.

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
pending, forwarded, or failed. A standard JS8Call ACK proves only the addressed
hop acknowledged the exchange. It never proves end-to-end delivery. A
non-enhanced recipient receives ordinary `DEST MSG readable text` and JS8Mail
reports no stronger result than the evidence supports.

Relays and custodians must preserve the `MID` and part metadata when forwarding
enhanced messages. They must deduplicate repeated data and receipts, respect
bounded hop/TTL policy, and never claim authentication beyond JS8Call's normal
radio evidence.

## Compatibility and safety

Malformed, oversized, unknown-version, or unknown-message frames are ignored
safely. All generated frames are validated before queueing. Implementations
must keep message contents local except for the RF transmission itself, and
must not depend on an Internet service. The syntax is compact metadata; the
underlying JS8Call speed, cycle timing, and token replacement determine actual
airtime.
