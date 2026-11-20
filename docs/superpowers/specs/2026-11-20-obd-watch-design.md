# OBD Watch: Continuous Raw CAN and RPM Recording While Cranking — Design

Date: 2026-11-20
Branch: `feature/obd-watch`, built on `feature/mcp-server` (not yet merged to `main`).

## Context and why

The case under study (MX_Test_1: 2007 LY5 drivetrain in a 1987 FJ60) is a no-start that is assumed, not proven, to be a crank signal problem. The question the owner needs answered is: **does the PCM see the engine turning while it cranks?** Today an OBD read is a one-shot snapshot (codes, then one sample of each live PID, ~6 s), so it cannot show what the PCM reports *during* a crank. The owner wants "as much and as granular data as we can get".

Facts from the vehicle and the code that shape this design:

- The truck answers on ISO 15765-4 CAN, 11-bit, 500 kbit/s (`ATDPN` → `A6`), and only the PCM answers (`7E8`). One request/reply round trip is ~85 ms in the traffic log, so PID polling tops out near 10 samples/s.
- The adapter is an OBDLink MX+ (STN2255 v5.12.4, reports ELM327 v1.4b). Its monitor mode records every frame on the bus without asking for anything. A GM PCM of this era broadcasts status frames every 10–100 ms, including (per community documentation, not official) engine speed on ID `0C9`.
- On the owner's Mac this MX+ answers only the **first** open of its serial port after Bluetooth pairing; a later open is silent until it is re-paired. So a watch must use the standing connection, never open its own port.
- `Controller` runs OBD reads as an `ObdJob` holding `_obd_lock`; the connection's heartbeat (`ATRV` every 5 s) skips while the lock is held. `SerialTransport.send` writes one allowlisted command and reads until the `>` prompt. `allowlist.check` refuses anything not explicitly listed. A case holds real or simulated OBD data, never both (`_wrong_kind`).
- Captures are saved per session (`captures/NNN.npz` + `.json`) and logged to the case. The MCP tool layer (`crank/mcp_tools.py`, on `feature/mcp-server`) gates control tools behind **Allow AI control**.

## Goals

- **Watch**: record the PCM continuously for a bounded time, in one of two modes:
  - **Raw** (default): passive CAN monitor. Every frame as the adapter printed it, laptop-timestamped. Best-effort decoding of known broadcast IDs (engine speed first).
  - **Poll**: repeated official OBD requests: Engine RPM (`010C`) every cycle and adapter voltage (`ATRV`) periodically.
- Start a watch on its own (Start/Stop, or a duration), **or** tick "Watch OBD during this capture" when arming the scope: the watch starts when the scope is armed and stops when the capture finishes, and the two are linked in the case.
- Save raw data to a file in the session; add one summary event per watch to the case, visible in the summary, timeline and report.
- Controls on the laptop page, the phone remote, and as MCP tools.
- Everything works against the simulator first, with simulated watches flagged `sim` like everything else.

## Non-goals

- Transmitting anything on the CAN bus. Not a request, not an acknowledgement. No `ATSH`, no `ATCSM0`, no ST transmit commands.
- Both modes at once in a single watch. Any byte sent to the adapter ends monitor mode, so it is one or the other.
- Receive filters, ID masks and SW-CAN/GMLAN single-wire. Monitor-all on the current protocol only.
- Microsecond timing or tooth-level alignment with the scope trace. Timestamps are laptop arrival times.
- Authoritative decoding of GM frames. Decoders are labelled best-effort and the raw frame is always kept.
- A watch without the standing connection.
- Fixing the MX+ "first open only" behaviour (separate issue).

## Anti-goals (failure even if it "works")

- A decoded value presented as fact. Best-effort values must say so everywhere they appear (UI, case, report, MCP).
- The adapter left in monitor mode, or with changed settings (`ATCAF0`, `ATD1`, …), after a watch ends, fails or is cancelled. The next read or heartbeat would get garbage.
- A stop sequence that can repeat the last command. On an ELM327, a bare CR repeats the previous command, which after a watch would restart monitoring.
- Losing frames silently. Overflow (`BUFFER FULL`), adapter resets and gaps must be detected and recorded.
- A simulated watch counted as evidence, or mixed into a case with real OBD data.

## Constraints

- **Read-only filter, loosened only for listen-only commands.** Owner approved 2026-11-20. Add exactly the commands monitor needs and each with a test. Expected set: `ATMA` (monitor all), `ATCSM1` (CAN silent monitoring: the adapter never ACKs), `ATCAF0`/`ATCAF1` (CAN auto-formatting off for monitor, back on after), `ATD0`/`ATD1` (show DLC), and `STMA` if the builder finds it preferable on the STN. `ATCSM0` and every transmit-capable command stay refused. The allowlist module docstring and README are updated to say what was added and why.
- **Stop byte.** Monitor mode is ended by writing one byte that is not CR, then reading until the prompt. This byte goes through a dedicated transport method, not `send`, and is the only unlisted byte the transport may write. If the adapter was already back at the prompt (it stopped itself on overflow), the stray byte must be cleared so it cannot prefix the next command. The builder chooses how (for example a trailing CR that yields a harmless `?`) and tests it.
- **Restore on every exit path.** Done, stopped, cancelled, overflow, timeout, exception. Leave monitor mode, then put back `ATCAF1`/`ATD0` (and anything else changed), then release the lock. If restoration fails, mark the connection cold (`mark_cold`) so the next read re-runs setup.
- **Protocol must be known before monitoring.** A cold connection has `ATSP0` (automatic) and no protocol chosen yet. Before `ATMA`, the watch sends `0100` (already allowed) and checks `ATDPN` is a CAN protocol. If the vehicle does not answer, the watch fails with the existing "The vehicle did not answer. Is the key on?" message.
- **Locking.** A watch holds `_obd_lock` for its whole run, like a read: no reads, no heartbeat pings, no second watch. Connection status shows that a watch is running (`watching: true`).
- **Bounded.** A standalone watch: default 20 s, maximum 120 s, plus Stop. A capture-linked watch is bounded by the capture instead (see "Capture-linked watches"), not by the 120 s cap.
- **Low cranking voltage.** The adapter may brown out and reset mid-crank. A reset (`LV RESET`, `ELM327`/`STN` banner, or silence past a gap threshold) ends the watch as `stopped`, keeps what was recorded, notes the reason, and marks the connection cold. A dropped Bluetooth link takes the existing heartbeat/`dropped` path after the watch releases the lock.
- **One kind per case.** Apply the existing `_wrong_kind` rule to watches.
- **AI gate.** MCP watch start/stop tools require Allow AI control. Read tools for watches do not.

## Approaches considered

1. **A watch job in the existing OBD stack (chosen).** A new job type beside `ObdJob` that borrows the standing connection through `HeldTransport`, a streaming mode on the transport, and recording through `SessionStore`/`Case`. It reuses the lock, connection, traffic log, sim/real separation and gate. It is the only option that respects the MX+ "first open only" behaviour.
2. **A separate process or script opening the serial port.** Rejected: it would conflict with the standing connection and use up the MX+'s one working open after pairing.
3. **A generic CAN library (python-can, SLCAN).** Rejected: the MX+ speaks the ELM327/STN command set, not SLCAN, so there is nothing for such a library to talk to.

The owner chose: both modes with **raw as default** (B), and **standalone plus capture-linked** watches (C). Both are recorded here as the owner's decisions.

## Design

### Transport: streaming

`Transport` gains a streaming interface, implemented by `SerialTransport`, `SimTransport` and passed through by `DelegatingTransport`/`HeldTransport`/`LoggingTransport`:

- `stream(cmd, on_line, stop, max_seconds)`: send an allowlisted command that starts a continuous output (`ATMA`), then call `on_line(t, text)` for each line as it arrives (`t` = laptop `time.time()` at receipt), until `stop` is set, `max_seconds` passes, or the adapter ends the stream itself (prompt seen, `BUFFER FULL`, `STOPPED`, a reset banner). Then end monitor mode with the stop byte, read to the prompt, and return why it ended plus any trailing text.
- Partial lines are buffered until CR. Empty lines are skipped.
- `LoggingTransport` writes one `watch started` / `watch ended (<reason>, N lines)` pair to the traffic log, not every frame. Frames go only to the watch file.

### Watch job and controller

`crank/obd/watch.py` (new):

- `WatchJob` (like `ObdJob`: thread, `state` ∈ running/done/stopped/cancelled/error, `message`, `cancel()`, `stop()`, `seconds`, live counters: lines, frames, latest decoded RPM, max decoded RPM).
- `run_raw(transport, …)`: if the connection is cold, re-run the setup commands first; protocol check; set `ATH1` explicitly (do not rely on setup having left headers on: a reset clears them), `ATCSM1`, `ATCAF0`, `ATD1`; `stream("ATMA", …)`; restore. The job reports a `monitoring` flag once the first line after `ATMA` has arrived (or `ATMA` has been accepted with no error).
- `run_poll(transport, …)`: loop `010C` each cycle and `ATRV` every 4th cycle until stop or time, using the reader's existing PID parsing for the PCM (`7E8`). A busy or no-answer reply is a recorded sample with a reason, not a failure.
- Stop keeps what was recorded. Cancel discards: nothing is saved to the session or the case.

`Controller` gains `start_watch(mode="raw", seconds=20, label="", capture_id=None) -> str | None`, `stop_watch()`, `cancel_watch()`, `watch_job`, and watch fields in `connection_status()`. `start_watch` refuses unless the connection is `connected` ("Connect to the adapter first."). The existing "a read is running" refusal is extended to watches. `delete_session` refuses while a watch is running on that session.

#### Capture-linked watches

`Controller.arm(label, note, watch=False, watch_mode="raw")` with `watch=True`:

- **Order: watch first, then scope.** The watch starts and gets as far as `monitoring` (raw) or its first `010C` answer (poll) *before* the scope is armed. Only then is the scope configured and armed, so "Crank the engine now." (laptop, phone remote and the MCP reply) is never shown before the watch is recording. A cold connection's protocol search therefore happens before the prompt, not during the crank. *(Builder-check call: the start of the crank is the part that matters most.)*
- **If the watch cannot start** (no connection, lock busy, vehicle silent, kinds differ), the capture still arms and runs, and the reason is shown next to it. The watch is never silently skipped.
- **Kinds must match.** A simulated scope capture (`Controller.mode == "Simulator"`) is only linked to a simulated watch, and a real capture only to a real watch. A mismatch is a "cannot start" reason as above. *(Builder-check call, from the sim-never-evidence anti-goal.)*
- **When it ends.** The watch stops at trigger time + the capture's record seconds + 2 s tail. It does not wait for downloading or analysis. If the capture ends without a trigger (timeout, error, cancel), the watch stops at that moment. It has no 120 s cap of its own: it is bounded by the capture's wait (default 120 s) plus record time plus tail. *(Builder-check call.)*
- **Cancelling the capture stops the watch (keeps it), not cancels it.** A watch is only discarded by its own Cancel. *(Builder-check call: the frames of a crank are evidence even if the scope part failed.)*
- **Cross-references.** When both are saved, the capture's metadata gets `watch_id` and the watch's gets `capture_id`. If the capture saved nothing, the watch is kept as a standalone watch with the note "linked capture did not save: <reason>".
- **Trigger time.** The watch metadata records the capture's trigger time (laptop clock, when the capture state became `triggered`) so the RPM chart can mark it.

### Decoding (best-effort)

`crank/obd/canframes.py` (new): parse a monitor line (`7E8 8 06 41 0C ...` style, headers on, DLC on, spaces off/on tolerated) into `(can_id, dlc, data)` or `None`, keeping the raw text either way. A decoder table maps an ID to named signals:

| ID | Signal | Decoding | Status |
|---|---|---|---|
| `0C9` | Engine speed | data bytes 1–2, **counted from 0** (byte 0 is a status byte), big-endian, × 0.25 rpm (community-documented for GM E38/E67 era PCMs) | best-effort |

The table starts with engine speed only. Each decoder has a version string (for example `0C9-engine-speed-v1: bytes[1:3] BE x0.25`) that is saved in the watch metadata, so a later correction can re-decode the saved raw frames. *(Builder-check call: zero-based is the community layout; it is unverified on this truck, which is why it is versioned and best-effort.)* Adding entries later is a data change. Every decoded value carries `best_effort: true` and the source ID. Poll-mode RPM comes from `010C` (official) and is labelled as such.

### Storage

`Session` gains `add_watch(meta, frames) -> watch_id` and `list_watches()`, `load_watch(id)`: `watches/NNN.jsonl` (one JSON object per line: `t`, `raw`, and when parsed `id`, `dlc`, `data` hex; poll samples: `t`, `cmd`, `raw`, `value`, `unit`, `reason`) and `watches/NNN.json` (id, label, mode, start/end times, seconds, end reason, sim, adapter, protocol, counts, gaps, capture_id, decoder version). Raw lines are written as they arrive (buffered file writes), so a crash keeps what was recorded.

**Summary computed at the end:** frames and lines total; per-ID counts and average rate (the "ID inventory"); largest gap between frames; overflow/reset notes; decoded engine speed (first nonzero time, max, median of nonzero) or, for poll, RPM samples and min/max adapter voltage.

### Case

`EVENT_TYPES` gains `watch`. `Case.append_watch(meta, summary)` writes one event: text like "OBD watch (raw CAN, 18.4 s): 1,512 frames from 14 IDs; engine speed (best-effort, ID 0C9) max 212 rpm", data = the summary + `sim` + `capture_id`, refs = `{watch: id, capture: id?}`. `summary.py`, `render.py` and the report show watches in the timeline with their best-effort label and the sim flag, and link a capture-linked watch next to its capture. `obd_kinds` counts watches as well, so the one-kind rule covers them.

### UI

- **Laptop** (`case_ui.py` OBD area): a "Watch" block: mode (Raw CAN, default / Poll RPM), seconds, Start, Stop, Cancel; live line/frame count and latest decoded RPM while running. After it finishes: the summary, an RPM-over-time chart (engine speed decoded or polled; if linked, a marker at the capture's trigger time), and the ID inventory table. A "Watch OBD during capture" checkbox (and mode) beside Arm.
- **Phone remote**: the same checkbox beside ARM, and a compact Watch Start/Stop with the live count.
- Disabled with a reason when not connected or busy.

### MCP tools

On `feature/mcp-server`'s `Tools`:

| Tool | Gate | Does |
|---|---|---|
| `obd_watch(mode="raw", seconds=20, label="")` | yes | start a watch; returns its state |
| `obd_watch_stop()` / `obd_watch_cancel()` | yes | stop (keep) / cancel (discard) |
| `watch_progress()` | no | state, seconds, counts, latest decoded value |
| `list_watches(session=None)` | no | id, label, mode, time, sim, headline |
| `get_watch(watch_id, session=None, max_points=500)` | no | meta, summary, ID inventory, downsampled engine-speed series; never the full frame list |
| `arm_capture(..., watch=False, watch_mode="raw")` | yes | existing tool, new parameters |

The server instructions gain one line: decoded CAN values are best-effort, and a watch's RPM is evidence about what the PCM broadcast, labelled as such.

### Simulator

`SimTransport.stream` produces realistic monitor output for the current scenario: a PCM broadcasting a handful of IDs at 10–100 ms, with `0C9` engine speed rising to ~150–250 rpm between seconds 2 and 8 ("cranking") in `healthy`. New scenarios: `no_rpm_while_cranking` (0C9 stays 0), `monitor_overflow` (ends with `BUFFER FULL`), `reset_mid_watch` (LV RESET banner mid-stream). Poll mode in sim returns the same RPM curve from `010C`.

## Testing

Unit tests with the sim and fake serial objects (the existing `serial_factory` hook), no hardware:

- Allowlist: each new command allowed; `ATCSM0`, `ATSH…`, transmit and other unlisted ST commands still refused.
- Stream: line splitting across reads; ends on stop, timeout, prompt, `BUFFER FULL`, reset banner. The stop byte is never CR. The stray-byte case is cleared.
- Restore runs on every exit path (done, stop, cancel, exception); on restore failure the connection is cold.
- Lock: watch blocks reads, heartbeat and a second watch; released on every path.
- Capture-linked: the scope is armed only after the watch is monitoring; the watch stops at trigger + record + tail, or when an untriggered capture ends; a failed watch start does not block the capture; capture cancel stops (keeps) the watch; sim/real mismatch refused; cross-references written, and the "did not save" note when the capture has no saved id.
- Storage, case event, summary/report rendering, one-kind rule, MCP gate and tool outputs.
- Decoder: `0C9` parsing, malformed lines kept raw and not decoded.

**Manual check on the truck (the real success test):** with the standing connection up and the key on, a 20 s raw watch records frames from the PCM, the adapter is answering normal reads afterwards, and a crank shows (or does not show) decoded engine speed. Then the same with a capture-linked watch.

## Left to the builder

- Exact monitor command (`ATMA` vs `STMA`) and the final listen-only command list, within the constraints above.
- The stop byte, and how a stray byte is cleared.
- Gap threshold for "the adapter went silent" (suggest 1 s with frames expected every ≤100 ms), poll `ATRV` cadence.
- File write buffering, chart library (reuse what the app already uses), downsampling method.
- Exact UI layout within the existing pages.
