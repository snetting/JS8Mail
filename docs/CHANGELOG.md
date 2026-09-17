# Changelog

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
