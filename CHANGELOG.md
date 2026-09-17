# Changelog

## 0.0.8 — 2026-09-17

Approaching the first stable community release. This release improves the
operator's understanding of route evidence and makes the message graph useful
for reviewing what was actually observed versus what JS8Mail attempted:

- Added semantic message-route graph edges for delivered, pending, failed,
  reported, and aged/observed evidence, with compact latest-route details.
- Kept remote route reports separate from local delivery attempts, avoiding a
  false impression that a complete path succeeded merely because a relay
  reported hearing the destination.
- Improved graph readability with straight edges, collision-aware labels,
  hop badges, labels drawn above edges, and node circles sized for long
  callsigns.
- Color-coded the Live RF Activity legend to match reciprocal, active one-way,
  aged one-way, and JS8Mail evidence links.
- No database migration is required for this release; existing observations,
  attempts, transactions, and path history remain usable.
- Completed standard JS8Call activity streams now reach normal inbox handling
  after reassembly; the activity mirror guard no longer discards a reconstructed
  final `RX.DIRECTED` event, so temporary partial entries can become complete.

Special thanks to F4LPU for reporting the overlapping route labels and small
callsign circles in the message graph. Thanks also to everyone who helped with
the local and on-air tests that exposed the edge cases behind this release.

This remains early-development experimental radio software, but the project
is approaching a first stable release for wider supervised community testing.

## 0.0.7 — 2026-09-16

Approaching the first community-testable release. This release includes the
following stability and interoperability work:

- Reassembled long standard and JS8Mail activity streams, including
  interleaved frames, partial messages, missing-part requests, and bare
  multiframe `J8M1 DELIVERED` receipts.
- Correlated standard hop acknowledgements, selective multipart acknowledgements,
  custody events, and end-to-end JS8Mail receipts without claiming more than
  the observed evidence proves.
- Preserved original senders and subjects through direct, relay, and
  store-and-forward paths, while keeping direct enhanced frames compact.
- Added route scoring, alternate-path retry, durable band-scoped observations,
  airtime protection, TX serialization, receive guards, and recovery from
  JS8Call/API busy or partial-frame conditions.
- Improved capability learning from first-contact `[JS8MAIL/x.y.z]` markers,
  overheard `J8M1 CAP` exchanges, and JS8Mail frames. Complete direct marked
  messages now trigger a delayed, rate-limited CAP response and bilateral
  capability confirmation without making ordinary delivery depend on it.
- Removed the readable JS8Mail marker from group broadcasts to reduce airtime;
  group traffic never participates in CAP negotiation.
- Made the Standard per-message override a full JS8Mail opt-out: it neither
  emits a marker nor initiates or waits for capability discovery.
- Stabilized the web UI: compact message previews, explicit subjects and
  protocols, inbox selection with visible selected rows, bulk read/delete
  actions, stable scrolling, active-band RF graphs, and clearer delivery
  states.
- Kept `Standard` as the protocol label for ordinary mail while adding a
  compact green capability dot when the sender has current evidence for future
  Opportunistic delivery.
- Kept the Recent observations panel useful during live operation by filtering
  high-volume radio plumbing while retaining partial-frame and capability
  evidence; aligned the release popup and Standard delivery wording with the
  current UI.

Extensive testing between two local stations covered standard delivery, JS8Mail
capability exchange, single-part and multipart delivery, partial-part
acknowledgement, receipt
reassembly, route evidence, and TX/RX pacing. Thanks to the operators who
helped with the on-air tests and feedback that exposed the edge cases behind
this release.

This remains early-development experimental radio software. Standard JS8Call
hop acknowledgement and custodian acceptance are not equivalent to an
end-to-end receipt; the UI keeps those outcomes distinct.
