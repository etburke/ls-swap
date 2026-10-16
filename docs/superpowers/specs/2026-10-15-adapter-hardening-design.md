# Adapter Timing and Error Hardening — Design

Date: 2026-10-15

## Context

Slice 3 of the epic "real OBD-II transports and hardening" (issue #4). Slice 1 (`2026-10-12-serial-obd-transport-design.md`, PR #12) added the serial transport and the read-only allowlist. Slice 2 (`2026-10-12-multi-ecu-reads-design.md`, PR #13) added per-ECU attribution. Both are merged. What remains before a first real read is how the reader behaves when the adapter is slow, asleep or unhappy: one flat 5 s timeout covers the first request (where the adapter searches for the protocol), every adapter error is a generic `ObdProtocolError`, and two reads can run at once on one serial port.

## Purpose and success

A read survives the ordinary hiccups of a real adapter and reports the rest in plain language, and every hiccup that happened stays visible in the case.

**Success:** against the simulator and a fake serial port, a slow protocol search completes, a single transient bus error is retried once and noted, an `LV RESET` re-initialises the adapter and the read continues, fatal adapter errors fail with a specific message, a sleeping adapter is woken, and a second read started during a running one is refused. A clean read looks exactly as before.

## Out of scope

Decided with the owner; each stays an unchecked item in #4:
- `STPTO` and per-command timeouts: they need `ST*` commands gated on a genuine STN adapter, which the adapter-identification slice builds.
- Job wrapper or spinner for long reads: page and phone-remote work, its own slice.
- Auto-reconnect after a drop: a dropped link is already reported, and a half-finished read has no state worth resuming.
- Nothing here is validated on a real adapter or truck (#3).

## Design

### Error taxonomy — `errors.py`, `protocol.py`

- `ObdAdapterError(ObdProtocolError)` carries `kind: str` and `transient: bool`. Existing `except ObdProtocolError` paths keep working.
- A table in `protocol.py` replaces the flat `ERRORS` tuple. Each row maps an adapter message prefix to a plain-language message and a class. Matching takes the longest prefix, so the catch-all `ERR` no longer swallows `ERROR` variants.

| Adapter says | Class | Behaviour |
|---|---|---|
| `BUS BUSY`, `BUS ERROR`, `CAN ERROR`, `FB ERROR`, `<RX ERROR`, `DATA ERROR`, `FC RX TIMEOUT` | transient | one retry |
| `LV RESET` | reset | re-run init, then one retry; message points at adapter power on OBD pin 16 |
| `BUFFER FULL`, `OUT OF MEMORY`, `UART RX OVERFLOW`, `STOPPED`, `ACT ALERT` | fatal | fails immediately, specific message |
| `UNABLE TO CONNECT`, `BUS INIT` | no vehicle | fails with the "is the key on?" wording |

- `?` keeps its existing message.

### Timeout and retry — `reader.py`

- `ObdReader(transport, timeout=5.0, first_timeout=20.0)`. Only the first `0100` uses `first_timeout`. `SEARCHING...` is already filtered as noise, so no parsing change is needed.
- `_ask` for an OBD request catches a transient `ObdAdapterError`, pauses briefly, resends once. Retries are capped at 3 per read, so a flapping bus cannot loop. A second failure on the same request raises the specific error.
- `LV RESET` on a request re-runs `INIT`, then resends the request with `first_timeout` (the protocol search starts again), and counts as that request's one retry. `INIT` commands themselves are not retried.
- Every retry and reset is appended to `ObdReadout.notes`, for example "Retried 010C after a CAN error." A read that succeeded after an `LV RESET` still carries the note: it is evidence about the adapter's supply.
- `raw` keeps the text of the last attempt for each command; the note records that an earlier attempt failed.

### Sleep/wake — `serial_transport.py`

- The MX+ wakes on the Bluetooth connection, so the first `ATZ` can be lost. `open()` sends `ATZ` twice at the same baud before falling back to 38400, so a slow wake is not mistaken for a wrong baud rate. A port that answers neither still raises `ObdTimeout` after both bauds.

### Read lock — `controller.py`

- `Controller.read_obd` takes a non-blocking lock. If a read is running it returns `(None, "An OBD read is already running.")`. The lock is released in a `finally`.

### Simulator — `transport.py`

- `SimTransport.send` honours `timeout`: a scenario with `search_delay` longer than the timeout it is given raises `ObdTimeout`, so the reader's two timeouts are exercised.
- New scenarios: `slow_search` (first reply takes longer than 5 s, shorter than 20 s), `transient_can_error` (one `CAN ERROR`, then normal), `persistent_can_error`, `lv_reset_mid_read`, `buffer_full`.

## Testing

- `test_obd_protocol.py`: every table row maps to the right class, message and `transient` flag; longest prefix wins; `ERROR`-prefixed text is not swallowed by `ERR`.
- `test_obd_reader.py`: `slow_search` succeeds and a 5 s `first_timeout` fails it; `transient_can_error` retries once and notes it; `persistent_can_error` raises the specific error after one retry; `lv_reset_mid_read` re-runs init, succeeds and notes the reset; `buffer_full` fails without retry; the retry cap holds; a clean read has no notes; the allowlist drift test still covers every command sent, including the re-init.
- `test_obd_serial.py`: fake port that ignores the first `ATZ` is woken by the second; a port that answers neither still raises after both bauds.
- `test_controller*.py`: a second `read_obd` during a running one is refused with the message; the lock is released after an error.
- Not covered: a real adapter, a real sleep cycle, a real brown-out.
