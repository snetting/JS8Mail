# JS8Call API capability matrix

This is the initial source-based matrix. `Runtime` remains `unknown` until a
live, non-transmitting probe is run against each installed build. A capability
must not be inferred solely from an application version string.

| Requirement | API event/action | Stock 2.3.1 | Improved 2.4.0 source | Runtime | Adapter policy |
|---|---|---:|---:|---:|---|
| Keepalive | `PING` | documented | present | unknown | read-only probe |
| Frequency | `RIG.GET_FREQ` / `RIG.FREQ` | documented | present | unknown | read-only snapshot |
| Frequency change | `RIG.SET_FREQ` | documented | present | unknown | disabled until explicitly approved |
| PTT observation | `RIG.PTT` | documented | emitted | unknown | evidence only |
| PTT query | `RIG.GET_PTT` | unknown | absent in inspected source | unknown | optional gate |
| Immediate halt | `RIG.TX_HALT` | unknown | absent in inspected source | unknown | never assume on 2.x |
| Station identity | `STATION.GET_CALLSIGN` | documented | present | unknown | startup read-only probe |
| Station grid/info/status | `STATION.GET_GRID`, `STATION.GET_INFO`, `STATION.GET_STATUS` | documented | present | unknown | startup read-only probe |
| Version | `STATION.VERSION` | unknown | absent in inspected source | unknown | probe if available |
| Receive activity | `RX.ACTIVITY` | documented | emitted | unknown | passive capture |
| Directed receive | `RX.DIRECTED` | documented | emitted | unknown | passive capture and parser input |
| Heard stations | `RX.GET_CALL_ACTIVITY` | documented | present | unknown | bounded snapshot |
| Band activity | `RX.GET_BAND_ACTIVITY` | documented | present | unknown | busy-channel evidence |
| Free offsets | `RX.GET_FREE_OFFSETS` | unknown | absent in inspected source | unknown | optional optimization only |
| Current RX text | `RX.GET_TEXT` | documented | present | unknown | diagnostics only |
| Current TX text | `TX.GET_TEXT` / `TX.TEXT` | documented | present | unknown | mandatory manual-activity guard |
| Automated send | `TX.SEND_MESSAGE` | documented | present | unknown | not exposed by receive-only client |
| TX queue depth | `TX.GET_QUEUE_DEPTH` | unknown | absent in inspected source | unknown | optional guard; unknown means defer |
| Speed | `MODE.GET_SPEED` / `MODE.SET_SPEED` | documented | present | unknown | read first; setting gated |
| Local inbox | `INBOX.GET_MESSAGES`, `INBOX.STORE_MESSAGE` | documented | present | unknown | local store integration later |
| API failures | `API.ERROR` | documented | present | unknown | bounded diagnostic event |

Sources:

- [JS8Call API documentation](https://js8call.com/JS8Call-improved/d7/d15/md_docs_2API.html)
- [JS8Call upstream](https://github.com/js8call/js8call)
- [JS8Call-improved 2.4.0](https://github.com/JS8Call-improved/JS8Call-improved/tree/release/2.4.0)

The receive-only client currently allows only read-only request construction.
Adding any transmit-capable request requires a separate adapter capability,
contract tests, and safety review.
