# Changelog

## 0.0.9 — 2026-09-19

This development milestone consolidates the recent reliability work before the
first stable community release:

- bounded autonomous TX deferral to three consecutive unrelated receive slots;
  traffic addressed to this station remains protected for the full message;
- clarified Outbox radio-handoff reasons instead of exposing generic
  `RuntimeError` labels, and kept selected routes while JS8Call is busy;
- moved expanded Outbox timelines and full content into a stable full-width
  detail row to reduce wrapping and layout movement;
- simplified station evidence labels to `Direct` and `Remote` while retaining
  the underlying reporter evidence for route scoring;
- retained the stored-message collection, duplicate suppression, and restart
  recovery improvements from the previous development milestone.

This remains early, experimental software. Thanks to the operators and
on-air testers who continue to expose real-world JS8Call timing and routing
cases.

## 0.0.8d — 2026-09-19

- fixed automatic collection after `YES MSG ID N` announcements, including
  structured and human-readable JS8Call activity forms;
- queued targeted retrieval outside the receive callback, with bounded retries,
  partial-message recovery, duplicate suppression, and restart restoration;
- documented the collection lifecycle and audit states.

## 0.0.8c — 2026-09-17

Outbox and delivery-safety follow-up:

- compacted repeated Outbox timeline entries, including repeated multi-line
  status blocks, while retaining the complete durable history;
- preferred untried custodians after an ambiguous store timeout;
- quarantined custodians after two unanswered offers, with escalating
  quarantine windows for repeated failures;
- quarantined exact relay paths after two completed RF timeouts without
  penalizing local/API, busy-radio, airtime, or incomplete-TX failures;
- kept alternate route discovery moving instead of waiting for a same-custodian
  cooldown.

This remains early, experimental radio software. Thanks to the operators and
on-air testers whose reports continue to improve its reliability.

## 0.0.8b — 2026-09-17

Emergency scheduler fix discovered during supervised testing:

- removed the erroneous station-wide cumulative airtime lifetime cap that
  could block all queued RF until the daemon restarted;
- retained the per-message 10-minute burst and 60-minute lifetime limits;
- calculated the next eligible airtime window for budget deferrals, avoiding
  unnecessary full-window waits and repeated blocked attempts;
- accepted beta version markers such as `[JS8MAIL/0.0.8b]`.

Thanks to the operators and on-air testers whose observations exposed this
failure mode. This is still an early, experimental release.

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
