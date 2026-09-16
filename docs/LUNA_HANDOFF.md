# Luna handoff

The current RF/protocol changes pass local tests but still need two-station
on-air validation before a community release.

1. Test CAP discovery in both directions, overheard CAP learning, enhanced
   multipart PA/DELIVERED receipts, and recovery after collisions or restart.
2. Test relay and store/forward paths, including custody versus final delivery
   status and missing-part recovery. Record actual JS8Call events for
   deterministic regression fixtures.
3. Validate RF-train completion and reply timing at different JS8Call speeds.
   Keep automatic speed changes opt-in until receiver-reported SNR and
   asymmetric-speed timing are validated.
4. Add Inbox bulk selection with **Mark as read** and **Delete** actions.
5. Exclude `@HB` from the **Groups and emergency alerts** panel. Heartbeat
   decodes should remain available to radio observations and routing evidence;
   only the user-facing group/alert catalog should hide this housekeeping
   group. Add a UI/API regression test so it does not reappear as an observed
   group or a subscribable alert.
6. Update the operator guide and protocol documentation to match verified
   current behavior. Explain the on-air overhead of CAP discovery, enhanced
   message IDs/part headers, cumulative PA replies, resend requests, and final
   receipts; distinguish estimated airtime from measured RF time and compare
   Standard, Opportunistic, and Required modes. Include an explicit example
   of binding the web UI to a LAN address for access from another machine,
   after verifying the actual launcher option. Note that LAN access must be
   restricted to trusted hosts because the UI can control RF transmission;
   do not imply that binding to a LAN address adds authentication or TLS.
7. Complete the UI cleanup pass: keep expanded Outbox/Inbox content stable
   during polling without layout shifts or flicker; keep previews to a few
   lines until opened; ensure action buttons fit with consistent spacing;
   preserve expanded state; keep the sticky status header and RX/DCD/TX/ERR/
   JS8 indicators clear; and make Compose/Outbox navigation land on the full
   target panel. Verify the live RF graph remains straight-lined, correctly
   colored, sized to its container, and clearly distinguishes observed,
   attempted, reciprocal, and JS8Mail-related links.
8. Finish mailbox usability details: show concise protocol/status pills,
   unread highlighting, reply and delete actions, and the planned bulk
   selection actions (**Mark as read** and **Delete**). Keep group alerts in
   their separate alert inbox and hide housekeeping `@HB` from the user-facing
   Groups and emergency alerts catalog.
9. Before live RF testing, verify the remote JS8Call instance remains active
   during idle periods and that the laptop does not suspend or stop audio/API
   processing on AC power. Record the JS8Call idle/heartbeat settings used;
   otherwise a missing response must be classified as an unavailable station,
   not immediately as a JS8Mail routing or receipt defect.
10. Verify unattended-operation guidance: JS8Call's operator-idle timer
    (variously labelled **My Station Idle Time**, **Idle Timeout**, or
    `TxIdleWatchdog`) must be disabled for overnight automation. Confirm that
    API polling does not falsely claim to reset it; JS8Mail must detect and
    report the resulting loss of automatic TX rather than create dummy RF
    traffic.
