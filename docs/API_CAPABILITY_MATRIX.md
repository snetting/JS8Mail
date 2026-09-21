# JS8Call API capability matrix

This matrix describes the adapter boundary used by the current `0.1.0`
daemon. `Runtime` is intentionally `unknown` where behaviour depends on the
installed JS8Call build; a capability must not be inferred solely from an
application version string. The live installation used during development has
successfully connected on TCP/2442, captured directed/RX/PTT/TX events, and
accepted automated text submissions, but that result is not a compatibility
claim for every JS8Call release.

| Requirement | API event/action | Stock 2.3.1 | Improved 2.4.0 source | Runtime | Adapter policy |
|---|---|---:|---:|---:|---|
| Keepalive | `PING` | documented | present | unknown | read-only probe |
| Frequency | `RIG.GET_FREQ` / `RIG.FREQ` | documented | present | unknown | read-only snapshot |
| Frequency change | `RIG.SET_FREQ` | documented | present | unknown | disabled until explicitly approved |
| PTT observation | `RIG.PTT` | documented | emitted | unknown | evidence only |
| PTT query | `RIG.GET_PTT` | runtime-dependent | runtime-dependent | optional | optional safety gate; failure falls back to TX text check |
| Immediate halt | `RIG.TX_HALT` | runtime-dependent | runtime-dependent | optional | not used by normal delivery; reserved for explicit operator/emergency control |
| Station identity | `STATION.GET_CALLSIGN` | documented | present | unknown | startup read-only probe |
| Station grid/info/status | `STATION.GET_GRID`, `STATION.GET_INFO`, `STATION.GET_STATUS` | documented | present | unknown | startup read-only probe |
| Version | `STATION.VERSION` | unknown | absent in inspected source | unknown | probe if available |
| Receive activity | `RX.ACTIVITY` | documented | emitted | unknown | passive capture |
| Activity frame boundaries | `RX.ACTIVITY.params.BITS` (`&1` first, `&2` last) | runtime-dependent | present in improved API change | unknown | conservative multi-stream reassembly; unknown bits ignored |
| Directed receive | `RX.DIRECTED` | documented | emitted | unknown | passive capture and parser input |
| Heard stations | `RX.GET_CALL_ACTIVITY` | documented | present | unknown | bounded snapshot |
| Band activity | `RX.GET_BAND_ACTIVITY` | documented | present | unknown | busy-channel evidence |
| Free offsets | `RX.GET_FREE_OFFSETS` | unknown | absent in inspected source | unknown | optional optimization only |
| Current RX text | `RX.GET_TEXT` | documented | present | unknown | diagnostics only |
| Current TX text | `TX.GET_TEXT` / `TX.TEXT` | documented | present | unknown | mandatory manual-activity guard |
| Automated send | `TX.SEND_MESSAGE` | documented | present | observed in live testing | serialized, airtime-limited automatic submission; manual TX text is never overwritten |
| TX queue depth | `TX.GET_QUEUE_DEPTH` | runtime-dependent | runtime-dependent | optional | optional guard; unknown means defer |
| Speed | `MODE.GET_SPEED` / `MODE.SET_SPEED` | documented | present | GET observed; SET build-dependent | read first; recommendations are safe fallback when SET is unavailable |
| Local inbox | `INBOX.GET_MESSAGES`, `INBOX.STORE_MESSAGE` | documented | present | unknown | local store integration later |
| API failures | `API.ERROR` | documented | present | unknown | bounded diagnostic event |

Sources:

- [JS8Call API documentation](https://js8call.com/JS8Call-improved/d7/d15/md_docs_2API.html)
- [JS8Call upstream](https://github.com/js8call/js8call)
- [JS8Call-improved 2.4.0](https://github.com/JS8Call-improved/JS8Call-improved/tree/release/2.4.0)

The current daemon has a deliberately narrow transmit surface: it uses
`TX.SEND_MESSAGE` only after validation, pacing, airtime checks, connection
checks, and a manual-text guard. It does not use `TX.SET_TEXT` as its normal
send path and does not use `RIG.TX_HALT` to terminate enhanced messages. A
`TX.FRAME` event is emitted while JS8Call is preparing a frame, so halting at
that event can truncate the frame before useful RF is sent. JS8Mail waits for
the complete observed PTT train to settle, then starts the receipt window and
protects an RX interval. The standalone `js8mail-probe` remains receive-only
and is useful for API diagnostics.

Some JS8Call 3.0.3 responses use a server-generated numeric `_ID` rather than
echoing the request identifier supplied by the client. The adapter therefore
uses the exact expected response type as a fallback only when there is one
outstanding read-only request, and turns `API.ERROR` into a failed request.
