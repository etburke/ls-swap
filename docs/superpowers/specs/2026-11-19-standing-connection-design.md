# Standing OBD Connection, Status, Relink and Traffic Log — Design

Date: 2026-11-19

## Context

Slice 7 of the epic "real OBD-II transports and hardening" (issue #4). Slices 1 to 6 (PRs #12, #13, #15, the identification slice, #16 and #17) added the serial transport, per-ECU attribution, timing and error hardening, adapter identification, per-PID outcomes, and cancelling or stopping a read. The first real reads, on an OBDLink MX+ connected to the truck, worked.

Today:

- Every read opens the serial port, resets the adapter (`ATZ`), identifies it, runs the protocol search, reads, and closes the port. The app cannot say whether the adapter is reachable until a read is tried, a drop is only discovered by a failed read, and every read repeats the reset, identification and search (several seconds on the truck).
- Nothing logs the adapter traffic. A cancelled or failed read throws its raw replies away (`readout.raw` exists only on success), so a failed cranking test cannot be diagnosed afterwards.
- The next slice (a continuous "ARM" OBD capture) and issue #8 (passive bus monitoring, scope time alignment) both need the port held open while data flows.

## Purpose and success

The owner presses **Connect** and sees that the adapter is up, its identity and battery voltage, and whether it is still there; presses **Relink** when it drops; and can watch every command and reply live. Reads on an open connection are fast, and every read, finished or not, leaves a trace in the log.

**Success:** against the simulator and fakes:

- Connect, Disconnect and Relink work from the sidebar and the phone page and show the state, the adapter label, the voltage, "last heard" and what the last read learned about the vehicle.
- A heartbeat notices a drop within about 10 s, closes the port and shows "Dropped".
- A read on the open connection skips the reset and identification, and still works with the existing cancel and stop behaviour.
- A read with no connection behaves exactly as today.
- Every command, reply, error and connection event is written to `logs/obd.log` and, for the active session, to `sessions/<name>/obd.log` (heartbeats only in the app-wide file); a **Log** page tails it live.
- Case files, reports and every existing caller of `Controller.read_obd` and `start_obd` behave as before.

## Out of scope

Decided with the owner; each stays an item in #4 or #8 unless noted:
- The continuous ARM capture (the next slice, built on this connection).
- Automatic reconnect after a drop: Relink is manual. Connect and Disconnect are the owner's (no auto-connect, no always-on).
- BLE, a second adapter, and changing what a read records in the case.
- Putting the log in the case or the report. It is diagnostic only.
- Probing the vehicle on connect: the protocol search stays in the first read (vehicle status reads "not checked" until a read finds out).
- `STPTO`, `STPX`, `STBC` and per-command timeouts; the clear-codes action; the headerless-parser minor.
- Nothing here is validated on the real adapter or truck beyond the earlier reads (#3).

## Design

### Allowlist and simulator

- `ATRV` is added to `AT_ALLOWED` (read-only: it returns the adapter's measured supply voltage and does not touch the vehicle bus). The drift test covers it.
- `SimTransport` answers `ATRV` with a voltage ("12.4V"). A new scenario field `atrv_fail_after: int | None` makes `ATRV` time out after that many answers, and a new scenario `heartbeat_drop` uses it, so Dropped and Relink can be tried without hardware. This makes 31 scenarios.

### ObdConnection

New `crank/obd/connection.py`.

- State: `disconnected`, `connecting`, `connected`, `dropped`, `error` (with a message).
- Facts it keeps: the transport, the `AdapterInfo`, the latest voltage and the time it was read, the time of the last good reply, the adapter source and kind (`real` or `simulated`), `cold` (the next read must do a full setup), and what the last read learned about the vehicle: `not checked`, `answered` (with a time) or `no answer`.
- `connect(source)`: opens the transport, runs the setup commands (`ATZ`, `ATE0`, `ATL0`, `ATS0`, `ATH1`, `ATSP0`), identifies the adapter, reads the voltage with `ATRV`, and starts the heartbeat. It does not send an OBD request, so it never waits for a protocol search. A failure leaves the state `error` with the message and the port closed.
- `disconnect()`: stops the heartbeat and closes the port. `relink()`: `disconnect()` then `connect()` with the same source.
- Connect, Disconnect and Relink are refused while a read is running, and Connect while already connected.

### Heartbeat

- A daemon thread sends `ATRV` every 5 s (configurable for tests), only when it can take the read lock without blocking, so it never interleaves with a read, and releases the lock at once.
- A good reply updates the voltage and the "last heard" time. A reply that is not a voltage, `LV RESET` or a timeout counts as a miss. Two misses in a row, or a connection error, set the state to `dropped` and close the port; an adapter reset also marks the connection cold.
- Disconnect stops the thread and waits for a heartbeat command in flight to finish before closing.

### Reads on the connection

- `Controller.start_obd` uses the open connection when its state is `connected`; the source picker is ignored and the page says "Using the open connection". With state `dropped` it refuses with "The adapter connection dropped. Press Relink, or Disconnect to read without a standing connection." With no connection it opens and closes the port itself, exactly as today.
- The reader is handed the connection's transport wrapped in a `HeldTransport` whose `open()` and `close()` do nothing, so `ObdReader.read()` keeps its flow and the port stays open.
- `ObdReader(..., adapter=info)` skips the setup commands and the adapter identification when `adapter` is given and uses it for the readout. It still sends `0100`, which runs the one-off protocol search on the first read and is quick afterwards. The `LV RESET` path in `_request` still re-applies the setup.
- After a read that is cancelled, stopped mid-command or failed, the connection is marked cold: the next read does the full setup. A read that finishes normally updates the vehicle status.
- The existing "one read at a time", cancel, stop and "real and simulated reads never mix" rules apply unchanged; a connection of one kind refuses a read of the other kind with the existing message.

### Controller

`connect(source) -> str | None`, `disconnect()`, `relink() -> str | None` (a message if refused or failed), `connection_status() -> dict` (state, message, adapter label, voltage, last heard, vehicle line, source kind, `reading`). The controller owns the connection, so the laptop and the phone share it.

### Traffic log

New `crank/obd/trafficlog.py`.

- `LoggingTransport(transport, log)` wraps any transport and records each command and reply with a timestamp and elapsed time, and errors with their type: `2026-11-19T16:02:15.123 [mx_test_2] >> 010C`, `… << 41 0C 0B B8 (43 ms)`, `… !! ObdTimeout: …`. Heartbeat commands carry a `hb` tag.
- Connection and read events go into the same log: connect, disconnect, relink, dropped, heartbeat miss, read started, finished, cancelled, stopped, failed.
- Both the standing connection and the per-read path are wrapped, so cancelled and failed reads leave a trace. Simulated traffic is logged and tagged `sim`.
- Files: `logs/obd.log` (rotates at about 5 MB, keeps 3 files) and `sessions/<name>/obd.log` for lines written while that session is active, without heartbeat lines. The writer holds a lock, and a write error never breaks a read or the heartbeat: it is counted and reported once in the log and in the status.
- The log lives outside the case: nothing in `events.jsonl`, the summary or the reports changes. It holds only allowlisted adapter commands and their replies.
- `tail_lines(path, n)` reads the last `n` lines from the end of the file in blocks, so a large log does not slow the page.

### UI

- **Sidebar status block** under the page picker: a coloured state line ("Connected, OBDLink MX+, 12.4 V, last heard 2 s ago", "Dropped", "Disconnected", the error text), the vehicle line, and **Connect**, **Disconnect**, **Relink**, with the existing adapter picker for Connect. It refreshes every 2 s with `st.fragment(run_every=2)`. Connect is disabled while connected, Disconnect while not connected, all three while a read runs.
- **Phone page:** the same status strip and buttons at the top of the remote page, from one shared helper in `case_ui.py` so the pages do not drift apart.
- **Case page and phone OBD panel:** when connected, a caption "Using the open connection" replaces the source picker's role.
- **Log page** (new entry in `PAGES`): the last 200 lines of `logs/obd.log`, newest at the bottom, refreshed every second with `st.fragment(run_every=1)`, a **Pause** toggle, a **Hide heartbeats** toggle (on by default), and the file path.

## Testing

All against fakes and the simulator, written test-first; heartbeat intervals are short in tests.

- **Allowlist/simulator:** `ATRV` passes and `ATRV` variants are still refused; the sim answers a voltage; `heartbeat_drop` stops answering after N; the drift test passes.
- **Connection:** connect, disconnect and relink states and messages; connect failure leaves `error` and the port closed; refused while a read runs; refused double connect; a drop is detected after two misses and the port closed; the heartbeat does not run while a read holds the lock; disconnect while a heartbeat is in flight; an adapter reset marks it cold; a good heartbeat updates the voltage.
- **Reads on the connection:** a warm read skips the setup and identification (commands sent) and records the connection's adapter; the first read still searches; a cancelled or failed read marks the connection cold and leaves the port open; a dropped connection refuses a read with the Relink message; a read of the other kind is refused; a read with no connection is unchanged.
- **Log:** line formats; heartbeat tag; connection and read events; rotation at the size limit; the session copy has no heartbeat lines; cancelled and failed reads appear; a write error does not break a read; `tail_lines` on small, large and empty files and a missing file.
- **UI:** the sidebar block in each state and its buttons' disabled states; the phone strip; the Log page tail, Pause and Hide heartbeats; the "Using the open connection" caption.

## Review focus

Inputs the tests above do not make obvious and a reviewer should check:

- A read started in the instant the heartbeat is sending: the read must wait or be refused, never interleave.
- Disconnect or Relink pressed from the phone while the laptop shows the Case page and a heartbeat is in flight.
- The adapter going away silently (power off) while connected and idle, and then Relink with the adapter still off: a clear error, not a hang.
- A cancelled read on a held connection: the port stays open, the next read runs a full setup and still works.
- Two browser sessions pressing Connect at once.
- The log growing without bound, or a log directory that cannot be written.
- `st.fragment(run_every=…)` reruns piling up while a long read blocks the page.
- The server stopping while connected: the daemon thread dies with the process and the OS closes the port.
