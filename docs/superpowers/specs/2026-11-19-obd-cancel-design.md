# Cancelling an OBD Read — Design

Date: 2026-11-19

## Context

Slice 6 of the epic "real OBD-II transports and hardening" (issue #4). Slices 1 to 5 (PRs #12, #13, #15, the identification slice and PR #16) added the serial transport, per-ECU attribution, timing and error hardening, adapter identification and per-PID outcomes. The first real read, on an OBDLink MX+ connected to the truck, worked.

What is missing is a way to stop a read:

- `Controller.read_obd` runs the whole read inside the Streamlit button click. The script blocks until the read ends, so no Cancel button can render, on the laptop or the phone.
- `CaptureJob` runs the scope capture in a thread and the UIs poll it, which is why capture has Cancel. The OBD read has no job.
- The reader has no cancel hook. One command can wait 5 s, the first request after connecting 20 s, and a read can run many commands.

## Purpose and success

A read in progress can be stopped from the laptop or the phone, promptly, and a stopped read leaves nothing in the case.

**Success:** against the simulator and fakes, a read started from either UI shows a running indicator with elapsed seconds and a Cancel button. Pressing Cancel ends the read within about 0.1 s even in the middle of a 20 s wait, closes the serial port, and records nothing: no event and no measurements. The UI says "Read cancelled; nothing was recorded". A read that is not cancelled records exactly what it does today. Every case file recorded before this slice, and every existing caller of `Controller.read_obd`, behaves as before.

## Out of scope

Decided with the owner; each stays an item in #4 unless noted:
- Recording a cancelled read in the case, or keeping a partial read. A cancel records nothing (owner's choice of the three options).
- Stage names or counts in the progress display. The indicator is "Reading from the vehicle… N s" only (owner's choice).
- Reconnect after a drop, `STPTO` and per-command timeouts, `STPX`, `STBC` batching.
- The clear-codes action, and the headerless-parser minor.
- Cancelling a scope capture, which already works, and stopping a read when the server process ends.
- Nothing here is validated on a real PCM beyond the one read already made (#3).

## Design

### Cancel signal

- `ObdCancelled(ObdError)` in `crank/obd/errors.py`. Its message is "Read cancelled; nothing was recorded."
- A `threading.Event` created by the job and passed to the transport and the reader as `cancel`. Both default to `None`, so every existing construction is unchanged.
- `SerialTransport.send`: the loop that reads in 0.1 s steps checks the event on each pass and raises `ObdCancelled`. It also checks before writing, so a cancel never starts a new command. The command already on the wire is a read-only allowlisted command and is simply abandoned.
- `ObdReader`: checks the event before each command (`_ask`) and during the retry pause (it waits on the event instead of sleeping, so a cancel ends the pause at once).
- The simulated transport answers instantly and needs only the reader's between-commands check.
- `ObdCancelled` is not an `ObdAdapterError`, so the reader's retry logic never retries it.
- After a cancel, the existing `except BaseException` path in `ObdReader.read()` closes the transport and re-raises. No adapter reset is sent.

### ObdJob

New `crank/obd/job.py`, modelled on `CaptureJob`.

- States: `idle`, `running`, `done`, `error`, `cancelled`. `running` is the only active state.
- Fields: `state`, `message`, `readout`, `started_at`, `session_name`, `source_kind`, and a `cancel` event. Properties: `running`, `seconds` (elapsed while running, fixed once finished).
- `start()` runs one thread. `cancel()` sets the event. `join(timeout=None)`.
- The worker calls a `run` callable passed in by the controller (the read and `append_obd`), and maps the outcome:
  - returns a readout → `done`;
  - raises `ObdCancelled` → `cancelled`, message as above;
  - raises another `ObdError` → `error`, message `str(e)`;
  - any other exception → `error`, message `"{type}: {e}"`, as `CaptureJob` does.
- A job that is `cancelled` or `error` has recorded nothing, because the append is the last step of `run` and only runs after a successful read.

### Controller

- `start_obd(source)` does the quick checks on the calling thread and returns an error message at once, or `None` once the job is started:
  1. Take the existing `_obd_lock` without blocking; otherwise "An OBD read is already running."
  2. Build the transport (`_obd_transport(source)`) and apply `_wrong_kind`; on `ObdError` or a wrong kind, release the lock and return the message.
  3. Capture the current `Case` (so switching sessions mid-read cannot send the result elsewhere) and create the `ObdJob` with a `run` that reads with `ObdReader(transport, cancel=job.cancel_event)` and then `case.append_obd(readout)`.
  4. The job releases the lock in its `finally`. A `threading.Lock` can be released from the worker thread.
- `cancel_obd()` cancels the current job if it is running.
- `obd_job` returns the latest job or `None`.
- `read_obd(source)` stays as a blocking wrapper: `start_obd`, then `join`, then `(readout, None)` or `(None, message)`. Its behaviour and the existing tests do not change.
- The job is held on the controller, so the laptop and the phone see the same read and either can cancel it.

### UIs

`case_ui._obd` and the phone remote's Read OBD expander use one shared helper in `case_ui.py` so the two pages do not drift apart.

- **Read from OBD** / **Read OBD** calls `start_obd(source)`; an immediate message is shown as an error. The button is disabled while a read runs.
- While a job runs: `st.info("Reading from the vehicle… N s")` and a **Cancel** button calling `cancel_obd()`, then a poll with `time.sleep` and `st.rerun()` using the same `CRANK_REMOTE_REFRESH_S` environment variable the remote already uses (tests set it to 0, which renders once and stops).
- When the job has finished, for the session it was started on only:
  - `done`: the existing "Recorded in the case:" success and result lines;
  - `cancelled`: `st.warning("Read cancelled; nothing was recorded.")`;
  - `error`: `st.error(message)`.
  The result stays until the next read starts. A job from another session shows nothing.

### Simulator

A new scenario `slow_adapter` makes each reply take about 1 s, so Cancel can be tried without hardware. The simulated transport gains a `delay` preset field (default 0) that sleeps before answering; `slow_adapter` sets it. This makes 30 scenarios. The README's scenario list and the cancel behaviour are documented.

## Testing

All against fakes and the simulator, written test-first.

- **Errors/transport:** `SerialTransport.send` raises `ObdCancelled` within about 0.1 s while waiting for a reply that never comes, and does not write a command once the event is set. A fake serial port drives this.
- **Reader:** a cancel before a command, in the middle of a read, and during the retry pause raises `ObdCancelled`, closes the transport, and is not retried. A reader with no event reads as before.
- **Job:** `done`, `cancelled`, an `ObdError` (`error`, message kept) and an unexpected exception (`error`, type in the message). Nothing is appended in the last three.
- **Controller:** `start_obd` returns the "already running", wrong-kind and transport-build messages without starting a job and without leaving the lock held; a started job holds the lock until it ends; `cancel_obd` ends a read and the case has no new event; `read_obd` still returns `(readout, None)` and `(None, message)` as before.
- **Simulator:** `slow_adapter` delays replies and is covered by the scenario-list tests; the allowlist drift test still passes.
- **App:** the Case page and the remote show the running indicator and Cancel while a gated read runs, then the cancelled message after Cancel, and the normal result after a completed read; a result from another session is not shown.

## Review focus

Inputs the tests above do not make obvious and a reviewer should check:

- Cancel pressed while the controller lock is being taken or released (the window between `start_obd` returning and the thread starting).
- Cancel arriving after the read finished but before `append_obd` completed: the read must either be recorded in full or not at all.
- A second browser tab or the phone pressing Read while the first read runs (must get "already running", not a second job).
- The server being stopped mid-read: the daemon thread dies with the process and the adapter port is closed by the OS.
- A cancelled read followed at once by another: the port must be reopened cleanly.
