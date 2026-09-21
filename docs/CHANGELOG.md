# Changelog

## 0.1.0 — 2026-09-21

This milestone brings the local mailbox, route discovery, operator UI, and
network-assisted discovery helper together for wider community testing:

- added the optional network route-hints integration, enabled by default, with
  short-lived band-scoped RF claims, expiry, indexed lookups, persistent
  service storage, a visible NET activity indicator, and graceful offline
  behavior; no message or mailbox data is shared;
- added network-hint provenance to route decisions, Outbox history, and message
  graphs while keeping fresh local RF evidence authoritative;
- completed the mail-first UI layout with a full-width Inbox, compact scrolling
  Outbox, stable expanded-history rows, bulk mailbox actions, readable group
  controls, active-band indicators, and capability/status LEDs;
- fixed the frontend refresh regression that could leave every data-backed panel
  stuck on “Loading…” after a JavaScript exception;
- retained the tested JS8Call handoff serialization, receive-window protection,
  route retention during radio-busy periods, custody collection recovery,
  multipart reassembly/resend handling, and bounded airtime scheduling;
- documented LAN UI access, JS8Call API setup, network route hints, privacy
  boundaries, and current experimental limitations.

This is still early, experimental radio software rather than a finished
emergency-service product. Extensive supervised local and on-air testing has
been performed. Thanks to F4LPU and to all operators who supplied reports,
test traffic, and real-world JS8Call observations.

## 0.0.9 — reliability milestone

- bounds autonomous TX deferral to three consecutive unrelated receive slots;
  traffic addressed to this station remains protected for the full message;
- reports meaningful JS8Call handoff reasons in the Outbox instead of bare
  `RuntimeError` names;
- renders expanded Outbox history in a stable full-width detail row;
- presents station evidence as `Direct` and `Remote` while retaining reporter
  evidence internally for routing decisions;
- carries forward the durable stored-message collection and restart-recovery
  fixes from 0.0.8d.

This remains early and experimental. Thanks to the operators and on-air testers
who continue to expose real-world JS8Call timing and store-and-forward cases.

## 0.0.8d — reliable stored-message collection

- recognizes JS8Call `YES MSG ID N` replies from both structured API events and
  human-readable activity lines such as `MM0ZFG: OH3SPN YES MSG ID 431`;
- queues the corresponding targeted `QUERY MSG N` outside the receive callback,
  so a reply is not lost while JS8Call is settling an RX/TX train;
- coalesces duplicate announcements, retries collection a bounded number of
  times, and keeps partial retrievals eligible for completion;
- restores pending collection jobs after a JS8Mail restart and records clear
  pending, submitted, failed, partial, completed, and exhausted audit events;
- bounds a blocked JS8Call handoff so stored-message collection cannot freeze
  the discovery scheduler.

This remains early and experimental. Thanks to the operators and on-air testers
who continue to expose real-world JS8Call timing and store-and-forward cases.

## 0.0.8c — Outbox and delivery-safety follow-up

- repeated Outbox timeline entries and repeated status blocks are now shown as
  compact `Repeated ×N` summaries while the full attempt history remains
  durable;
- custodians receive one short retry opportunity, then enter an escalating
  24-hour-or-longer quarantine after unanswered store offers;
- exact relay paths are quarantined after repeated completed RF timeouts, while
  local handoff and radio-busy failures are not attributed to remote stations;
- alternate route discovery continues without waiting for a same-custodian
  cooldown.

This remains early and experimental. Thanks to the operators and on-air testers
who helped identify these reliability issues.

## 0.0.8b — emergency scheduler fix

This small follow-up release addresses a serious airtime-accounting defect
found during supervised testing:

- removes the erroneous station-wide cumulative lifetime cap that could leave
  every queued message blocked until the daemon was restarted;
- keeps the intended per-message 10-minute burst and 60-minute lifetime
  protections;
- calculates the next eligible radio window instead of blindly deferring for
  another full 15 minutes;
- reduces repeated budget-block attempts and makes the resulting audit detail
  clearer.

The release remains early and experimental. The fix changes local scheduling
and accounting only; it does not alter the JS8M wire format. Thanks to the
operators and on-air testers who supplied the observations that exposed this
failure mode.
