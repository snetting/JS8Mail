# Airtime reservation and RF reconciliation: Luna handoff

Status: analysis and implementation plan only. Branch:
`airtime-reservation-reconciliation`. No radio or scheduler behavior is changed
by this document.

## Observed incident and corrected diagnosis

On 2026-09-17, group broadcast `7a2f57db5e5f0c4d` repeatedly logged
`rolling airtime budget exhausted at speed 0`, although the radio was idle.
The prior daemon retried the same blocked broadcast every five seconds. Commit
`b6714d6` made the group scheduler honor its defer deadline and distinguish
budget exhaustion from an occupied JS8Call TX slot. After the daemon restarted,
the broadcast was submitted once and its RF train completed.

The restart was the important clue. Immediately before it, the persisted
`airtime_usage` row had a window beginning at 11:52:38 local time, only
120,000 ms of window airtime, and **900,000 ms of `message_used_ms`**. The
15-minute window had expired long before the 15:03 rejection. The code gives
the **station-wide** `AirtimeBudget` both a 15-minute window cap and a
15-minute cumulative `message_limit_ms` cap. `rollover()` clears only
`window_used_ms`. Thus 900,000 ms of accumulated estimates permanently blocks
every outgoing RF action for that daemon's lifetime. Startup reloads the window
fields but omits `message_used_ms`, so restarting silently clears the block.
The previous explanation that the old 15-minute window merely needed to roll
over was incorrect. See `radio_policy.py:AirtimeBudget`, `tools/app.py:run`
and `tools/app.py:Handler._send_rf_serialized`.

This is a fixed 15-minute window anchored at its first spend, not a continuously
sliding 15-minute total. The current 15-minutes-per-15-minutes radio cap is a
100% allowance for estimated TX time; it mainly limits bursts and accidental
overcounting. Do not silently replace it with a different duty-cycle policy in
the accounting fix.

## Additional defects in the same path

1. `send_rf` charges station and message counters **before** the final local
   slot check and `TX.SEND_MESSAGE` write. A changed slot, occupied JS8Call
   text/queue, disconnected socket, or failed API write can leave a durable
   charge for RF that never began. `tx_reserved_ms` is cleared on error, but
   neither counter is refunded.
2. `Js8CallClient.send_message()` awaits socket `drain()`, not an explicit
   JS8Call acceptance acknowledgement. A successful return proves only that
   the request was written to the TCP stream. Queued, rejected, delayed, and
   actually transmitted are different outcomes.
3. On RF completion, `complete_rf_train()` adds only positive
   `actual_train_ms - status['tx_reserved_ms']`. It never releases an
   overestimate. `tx_reserved_ms` holds just the **last** `send_rf` reservation;
   a multi-frame train may already have been charged once per frame. Comparing
   the whole train against the last reservation can therefore double-charge
   earlier frames. Control traffic without a message ID still charges the
   station-wide counter but has no per-transaction reservation record.
4. TX.FRAME carries tones, not a message ID or original text, in the observed
   JS8Call events. `RIG.PTT` supplies the measured RF interval but does not
   identify who caused it. The current code completes the one active
   transaction, including a group broadcast, on a settled PTT train without
   proving that train belonged to that transaction. A manual or stale JS8Call
   TX could be misattributed.
5. A directed transaction without a final PTT event times out after 30
   minutes; an unfinished broadcast becomes unconfirmed and is not resent
   automatically. Neither path reconciles its airtime reservation. A later
   retry can charge again, and delayed JS8Call TX can still occur.
6. The group fix uses a fixed 15-minute defer after an airtime block. When a
   real window would become eligible sooner, it wastes the intervening TX
   opportunities. A failed broadcast attempt also records `failed` inside
   `transmit()` before the scheduler records `deferred`, confusing the UI.
7. Existing tests cover PTT train settling and a successful broadcast but do
   not cover the lifetime radio cap, failed-handoff refund, restart recovery,
   overestimate correction, multi-frame accounting, or uncertain queue state.
   The operator guide currently says counters persist across restart without
   explaining that one field is discarded on startup.

## Implementation sequence

### 1. Remove the permanent station-wide lock first

- Separate station window usage from per-message lifetime usage. The radio
  scope must not apply `message_limit_ms`; the one-hour lifetime ceiling stays
  keyed to each message, with its 10-minute burst cap.
- Make disabled limits explicit (`None` or a distinct budget type), rather than
  using a very large sentinel. Add a regression test that exceeds 15 minutes
  of cumulative station activity over multiple windows without blocking the
  next window. Restart must not change eligibility.
- In the migration, ignore the legacy radio `message_used_ms` for eligibility.
  Preserve it for diagnostics if useful, but do not treat it as a per-message
  debit. Preserve current-window usage only while that window is still valid.

### 2. Create a durable reservation ledger

- Add a DB migration for one row per intended RF handoff, linked to its
  transaction/message when applicable, with operation, band, estimated ms,
  created/submitted/start/end times, observed PTT ms, and a unique handoff ID.
  Include protocol controls such as CAP and QUERY MSGS, which may have no
  parent message. Record state transitions idempotently.
- Suggested states: `prepared`, `submitted_unknown`, `rf_active`,
  `rf_complete`, `rejected_before_submit`, `not_transmitted`, and
  `uncertain`. Keep the existing delivery/ACK transaction status separate:
  an RF-complete frame is not an end-to-end delivery receipt.
- Station eligibility should use **measured PTT airtime plus outstanding
  reservations** in the applicable window, never a permanent station
  lifetime counter. Per-message limits should use only that message's
  measured airtime plus its pending reservations. Serialize decisions with
  a scheduler lock and commit each ledger transition atomically. Do not keep
  a database transaction open across an awaited JS8Call API call.
- If a local preflight fails before any API write, release the reservation.
  If a write may have reached JS8Call, keep it `submitted_unknown` until queue
  and PTT evidence resolves it; do not assume it was rejected.

### 3. Reconcile PTT trains correctly

- Accumulate all handoff reservations belonging to a TX train. At settlement,
  replace their aggregate estimate with measured PTT time, releasing unused
  estimate or charging the excess once. For example, two 30-second frame
  reservations and a 40-second PTT train should charge 40 seconds, not
  60 + (40 - 30) = 70 seconds.
- Handle PTT across a window boundary by attributing actual milliseconds to
  the correct windows, or document a conservative equivalent. Repeated PTT,
  TX.FRAME, settle tasks, and daemon restart must not double-finalize a row.
- Do not mark a broadcast complete merely because an unrelated PTT train
  ended. Correlate using a single pending handoff and available queue/frame
  evidence; when attribution is ambiguous, record `uncertain` and leave the
  broadcast unconfirmed. Count measured station PTT even when ownership is
  unknown.

### 4. Handle accepted-but-silent JS8Call queues

- Query `TX.GET_QUEUE_DEPTH`, `TX.GET_TEXT`, and PTT when the installed build
  supports them. Record each probe's reliability; these APIs are optional.
  A zero queue plus empty TX text on repeated probes, with no intervening
  TX.FRAME/PTT, can support `not_transmitted` after a bounded start grace.
- Where queue state is unavailable or inconsistent, classify the handoff as
  `uncertain` after the grace period. Do not auto-resubmit the same wire text
  while a delayed JS8Call send is plausible. Provide an operator action to
  inspect/clear that one uncertain attempt and retry deliberately. A bounded
  reservation must not block unrelated mail indefinitely; define the point
  at which its budget hold expires without claiming the message never aired.
- Reconcile outstanding ledger rows on startup before scheduling new work.
  Preserve uncertainty across restart, and avoid both a phantom lifetime cap
  and duplicate queue submissions.

### 5. Schedule by actual eligibility time and make it visible

- Compute `next_budget_eligible_at` from the applicable window or ledger,
  plus a small pacing guard. Defer the message until that timestamp rather
  than always sleeping a fresh 15 minutes. Honor the deadline for *every*
  group and directed branch; keep a blocked message from adding five-second
  attempt rows. Let unrelated eligible messages proceed.
- Display `Radio busy`, `Airtime cooldown until …`, `Queued in JS8Call`,
  `TX observed`, and `TX unconfirmed` as distinct states. Show estimated
  pending, measured used, the active window's reset time, and per-message
  remaining allowance. The UI must not label a budget block as a TX-slot
  failure or an unobserved broadcast as complete.
- Update `docs/USER_GUIDE.md` with exact window and uncertainty semantics.

## Required deterministic tests

Use a fake clock, fake JS8Call API/queue, and captured PTT/TX.FRAME events:

1. Multiple windows exceed 15 minutes cumulatively; the next window still
   admits TX both with and without a daemon restart.
2. Preflight rejection and definite API write failure leave zero consumed
   airtime and no active reservation. A possibly written API request remains
   uncertain instead of being refunded or replayed immediately.
3. API write succeeds, no PTT occurs, queue becomes empty: reservation is
   released after evidence confirms no TX. Queue remains occupied or unknown:
   no automatic duplicate and a bounded, visible uncertain state.
4. Two reserved frames form one PTT train: measured airtime is charged once,
   including when it is shorter or longer than the aggregate estimate.
5. Unrelated/manual PTT during a pending JS8Mail handoff counts as station
   airtime but does not mark that broadcast delivered or debit its message.
6. Crash/restart at each lifecycle stage restores the same eligibility and
   does not resubmit a still-queued message.
7. Budget becomes available partway through a window: the waiting group post
   wakes at its computed deadline; no five-second log storm or full-window
   delay; another eligible message is not starved.
8. A group broadcast reaches `complete` only after its own correlated RF
   train; missing completion becomes `unconfirmed` without an automatic
   repeat. Direct/multipart receipt handling remains unchanged.

Validate with the repository's Ruff, mypy, and pytest CI matrix. After the
deterministic suite passes, use a short, supervised two-station RF test to
compare reservations, PTT measurements, queue status, and the displayed
eligibility time. Do not use a long or repeated live broadcast as the first
validation step.
