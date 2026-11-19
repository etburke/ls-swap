# Cancelling and Stopping an OBD Read — Design

Date: 2026-11-19 (revised the same day: Stop added)

## Context

Slice 6 of the epic "real OBD-II transports and hardening" (issue #4). Slices 1 to 5 (PRs #12, #13, #15, the identification slice and PR #16) added the serial transport, per-ECU attribution, timing and error hardening, adapter identification and per-PID outcomes. The first real read, on an OBDLink MX+ connected to the truck, worked.

What is missing is a way to end a read early:

- `Controller.read_obd` runs the whole read inside the Streamlit button click. The script blocks until the read ends, so no button can render, on the laptop or the phone.
- `CaptureJob` runs the scope capture in a thread and the UIs poll it, which is why capture has Cancel. The OBD read has no job.
- The reader has no interrupt hook. One command can wait 5 s, the first request after connecting 20 s, and a read can run many commands.

## Purpose and success

A read in progress can be ended from the laptop or the phone, promptly, in one of two ways: **Cancel** throws it away, **Stop** keeps what was collected and says plainly what was not.

**Success:** against the simulator and fakes, a read started from either UI shows a running indicator with elapsed seconds and two buttons.

- **Cancel** ends the read within about 0.1 s even in the middle of a 20 s wait, closes the serial port, and records nothing: no event and no measurements. The UI says "Read cancelled; nothing was recorded."
- **Stop** ends the read at the next command and records what had been read, flagged as stopped. Codes, freeze frame or live values that were not reached are shown as "not read" and are never shown as "none". The UI says "Stopped; kept what was read."
- **Stop before the PCM has answered anything** has nothing to keep, so it behaves as Cancel (and interrupts a wait as promptly).
- A read that is not ended early records exactly what it does today. Every case file recorded before this slice, and every existing caller of `Controller.read_obd`, behaves as before.

## Out of scope

Decided with the owner; each stays an item in #4 unless noted:
- Recording a cancelled read in the case. Cancel records nothing (owner's choice).
- Stage names or counts in the progress display. The indicator is "Reading from the vehicle… N s" only (owner's choice).
- Pausing and resuming a read, and Stop acting in the middle of a command (it acts at the next command boundary).
- Reconnect after a drop, `STPTO` and per-command timeouts, `STPX`, `STBC` batching.
- The clear-codes action, and the headerless-parser minor.
- Cancelling a scope capture, which already works, and stopping a read when the server process ends.
- Nothing here is validated on a real PCM beyond the one read already made (#3).

## Design

### Interrupt signals

- `ObdCancelled(ObdError)` in `crank/obd/errors.py`; message "Read cancelled; nothing was recorded." It is not an `ObdAdapterError`, so the reader never retries it.
- `ObdStopped(ObdError)`, internal to the reader: raised at a command boundary when Stop was pressed and there is data to keep. It never leaves `ObdReader.read()`.
- Two `threading.Event`s, `cancel` and `stop`, created by the job and passed to the reader. The reader hands the transport one small object whose `is_set()` is true when cancel is set, or when stop is set and the PCM has not yet answered anything. The transport only ever asks "should I give up waiting?".
- `Transport.cancel` (default `None`) and `Transport.check_cancel()` raise `ObdCancelled`.
- `SerialTransport.send` checks before writing and on every 0.1 s read pass, so an interrupt lands within about 0.1 s and no new command is written. `SerialTransport.open` closes the port if it is cancelled after connecting.
- `SimTransport` gains a per-reply `delay` (scenario field) and waits in 50 ms steps that check the interrupt.
- `ObdReader` checks before each command, before connecting and during the retry pause (which waits on the interrupt instead of sleeping). Cancel is checked first, so Cancel wins if both are set.
- After a cancel the existing `except BaseException` path in `read()` closes the transport and re-raises. No adapter reset is sent.

### Stop and the partial readout

- The reader tracks the section it is in: the support maps, the check-engine-light status (`0101`), each code mode (`03`, `07`, `0A`), the freeze frame (`02`) and the live values.
- A stop seen after the PCM has answered raises `ObdStopped`; `read()` catches it, finishes the readout and returns it normally (the transport is closed on the normal path).
- Sections already complete are kept. The section in progress is dropped whole: a code mode interrupted before it finished is not read; a freeze frame interrupted partway is not kept.
- `ObdReadout` gains `stopped: bool` and `not_read: list[str]`, the code modes (`03`, `07`, `0A`) and freeze frame (`02`) the read never completed.
- Live PIDs not reached are added with a new reason `stopped` ("not read, stopped by you"), so they appear under "Not read from the PCM" with the other missing values and a count in the result line.
- The readout gets the note "Stopped by you before the read finished.", which already prints in the result lines, the summary, the report and the HTML.
- `to_event_data()` adds `stopped` and `not_read`; events without them (every earlier case) read as not stopped.

### Presentation of a partial read

- Result lines: the codes line says "not read" when no code mode was read, and appends "(modes not read: 07, 0A)" when only some were; the freeze-frame line says "Freeze frame not read" when `02` is in `not_read`; the live line reads "Live values read: 4 of 10 (not read, stopped by you: …)".
- Case summary: the unread set is the union of `unsupported_modes` and `not_read`. Codes from an earlier read are not demoted to "not in latest OBD read" for a mode the latest read did not read, and the earlier freeze frame stays. The timeline line says "modes not read: …", and "codes not read" when no code mode was read, and ends with "; stopped by you before it finished".
- `Case.append_obd` is unchanged: a live value that was never read has no value, so no measurement is recorded for it. Live values are read in a fixed order with RPM first, so a stop never leaves other values recorded without RPM.

### ObdJob

New `crank/obd/job.py`, modelled on `CaptureJob`.

- States: `idle`, `running`, `done`, `stopped`, `error`, `cancelled`. `running` is the only active state.
- Fields: `state`, `message`, `readout`, `started_at`, `session_name`, `cancel_event`, `stop_event`. Properties: `running`, `seconds` (elapsed while running, fixed once finished). Methods: `start()`, `cancel()`, `stop()`, `join(timeout=None)`.
- The worker calls `run(cancel_event, stop_event)` and maps the outcome:
  - returns a readout → `stopped` if `readout.stopped`, else `done`;
  - raises `ObdCancelled` → `cancelled`, message as above;
  - raises another `ObdError` → `error`, message `str(e)`;
  - any other exception → `error`, message `"{type}: {e}"`, as `CaptureJob` does.
- A `cancelled` or `error` job has recorded nothing, because the append is the last step of `run`.
- `on_finish` (the controller's lock release) runs before the final state becomes visible, so a poller that sees the end can start the next read at once.

### Controller

- `start_obd(source)` does the quick checks on the calling thread and returns an error message at once, or `None` once the job is started:
  1. Take the existing `_obd_lock` without blocking; otherwise "An OBD read is already running."
  2. Build the transport (`_obd_transport(source)`) and apply `_wrong_kind`; on `ObdError` or a wrong kind, release the lock and return the message.
  3. Capture the current `Case` (so switching sessions mid-read cannot send the result elsewhere) and create the `ObdJob` with a `run` that reads with `ObdReader(transport, cancel=…, stop=…)` and then `case.append_obd(readout)`.
  4. The job releases the lock through `on_finish`. A `threading.Lock` can be released from the worker thread.
- `cancel_obd()` and `stop_obd()` act on the current job if it is running. `obd_job` returns the latest job or `None`.
- `read_obd(source)` stays as a blocking wrapper: `start_obd`, then `join`, then `(readout, None)` for `done` or `stopped`, else `(None, message)`. Its behaviour and the existing tests do not change.
- The job is held on the controller, so the laptop and the phone see the same read and either can end it.

### UIs

`case_ui._obd` and the phone remote's Read OBD expander use one shared helper in `case_ui.py` so the two pages do not drift apart.

- **Read from OBD** / **Read OBD** calls `start_obd(source)`; an immediate message is shown as an error. The button is disabled while a read runs. After starting, the page waits up to 0.5 s for the job so a quick read (the simulator) is finished before the page is drawn.
- While a job runs: `st.info("Reading from the vehicle… N s")` and two buttons side by side, **Stop (keep what was read)** and **Cancel**, then a poll with `time.sleep` and `st.rerun()` using the same `CRANK_REMOTE_REFRESH_S` environment variable the remote already uses (tests set it to 0, which renders once and stops).
- When the job has finished, for the session it was started on only:
  - `done`: the existing "Recorded in the case:" success and result lines;
  - `stopped`: "Stopped; kept what was read. Recorded in the case:" and the result lines, which show what was not read;
  - `cancelled`: `st.warning("Read cancelled; nothing was recorded.")`;
  - `error`: `st.error(message)`.
  The result stays until the next read starts. A job from another session shows nothing.

### Simulator

A new scenario `slow_adapter` makes each reply take about 1 s, so both buttons can be tried without hardware. This makes 30 scenarios. The README's scenario list and the cancel and stop behaviour are documented.

## Testing

All against fakes and the simulator, written test-first.

- **Errors/transport:** `SerialTransport.send` raises `ObdCancelled` within about 0.1 s while waiting for a reply that never comes, does not write a command once the event is set, and closes the port when cancelled during `open`. A fake serial port drives this.
- **Reader, cancel:** a cancel before connecting, in the middle of a read, and during the retry pause raises `ObdCancelled`, closes the transport, and is not retried. A reader with no event reads as before.
- **Reader, stop:** stop before the PCM answers raises `ObdCancelled`; stop after `0100`, after mode `03`, and during the live values each return a stopped readout with the right `not_read`, codes kept, and `stopped` reasons on the PIDs not reached; cancel wins over stop.
- **Presentation:** result lines, event data, summary (no demotion, "not read" wording, timeline line), markdown and HTML for a stopped read; no measurements for values not reached; old events without the new fields still render; the reason text and ordering include `stopped`.
- **Job:** `done`, `stopped`, `cancelled`, an `ObdError` (`error`, message kept) and an unexpected exception (`error`, type in the message). Nothing is appended in the last three.
- **Controller:** `start_obd` returns the "already running", wrong-kind and transport-build messages without starting a job and without leaving the lock held; a started job holds the lock until it ends; `cancel_obd` ends a read and the case has no new event; `stop_obd` after data keeps the read, and before data acts as a cancel; `read_obd` still returns `(readout, None)` and `(None, message)` as before.
- **Simulator:** `slow_adapter` delays replies and a cancel cuts the wait; the allowlist drift test skips it (it sends the commands `healthy` does).
- **App:** the Case page and the remote show the running indicator with both buttons while a gated read runs; Cancel shows the cancelled message and records nothing; Stop after data shows the stopped message and records one event; a result from another session is not shown.

## Review focus

Inputs the tests above do not make obvious and a reviewer should check:

- Cancel or Stop pressed straight after `start_obd` returns, before the thread has sent anything.
- Stop arriving exactly as the first `0100` is answered (the data/no-data boundary), and Stop and Cancel both pressed.
- A read that finished just as Cancel or Stop was pressed: it must be recorded in full or not at all, and a cancel after the read ended must not undo it.
- A stopped read after an earlier read of the same case: earlier codes and freeze frame must not be shown as gone.
- A second Read pressed from the phone or another tab while one runs: "already running", not a second job.
- `SerialTransport.open` cancelled after connecting must close the port.
- The server being stopped mid-read: the thread is a daemon and the OS closes the port.
