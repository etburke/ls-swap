# Cancelling and Stopping an OBD Read Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A running OBD read can be ended from the laptop Case page or the phone remote: Cancel (within about 0.1 s, records nothing) or Stop (keeps what was read and says what was not).

**Architecture:** `ObdCancelled` and an internal `ObdStopped`; two `threading.Event`s (cancel, stop) that the reader checks, with one small interrupt object the transport polls. An `ObdJob` runs the read in a thread (like `CaptureJob`); the controller holds it and exposes `start_obd` / `cancel_obd` / `stop_obd` / `obd_job`, keeping `read_obd` as a blocking wrapper. A stopped read is a normal readout flagged `stopped` with a `not_read` list, presented through the existing "modes not read" and "Not read from the PCM" paths. One shared UI helper draws the button, the running indicator, Stop, Cancel and the result on both pages.

**Tech Stack:** Python 3, `threading`, pyserial (lazy), Streamlit (`AppTest` for UI tests), pytest. Run tests with `python -m pytest` (the system `python` has pyserial; `.venv` does not).

**Spec:** `docs/superpowers/specs/2026-11-19-obd-cancel-design.md`

## Global Constraints

- Cancel records nothing: no event, no measurements. Message exactly `Read cancelled; nothing was recorded.`
- Stop after the PCM has answered keeps what was read; before that it behaves as Cancel. UI message for a kept read: `Stopped; kept what was read.`
- A section not read is shown as "not read", never as "none": code modes (`03`, `07`, `0A`) and freeze frame (`02`) go in `not_read`; live PIDs not reached get reason `stopped` ("not read, stopped by you").
- The note `Stopped by you before the read finished.` is added to every stopped readout.
- Progress display is "Reading from the vehicle… N s" plus the two buttons only: no stage names, no counts.
- `ObdCancelled` and `ObdStopped` are `ObdError`s but not `ObdAdapterError`s, so the reader never retries them. `ObdStopped` never leaves `ObdReader.read()`.
- An interrupt must land within about 0.1 s while `SerialTransport.send` waits for a reply; no new command is written once it is set. Cancel is checked before Stop.
- Every existing caller of `Controller.read_obd` and every existing test keeps working unchanged (`read_obd` still returns `(readout, None)` or `(None, message)`). Events without the new `stopped` / `not_read` keys read as not stopped.
- Simulator: 30 scenarios after this slice; the new one is `slow_adapter` (about 1 s per reply).
- Only allowlisted read-only commands are ever sent; this slice adds none.
- Commit only the files a task names. The working tree also holds an uncommitted, unrelated session-creation fix in `app.py` and `tests/test_app.py`: stage files by name, never `git add -A` or `git add .`. For `tests/test_app.py` use `git add -p` and take only this task's hunks if that fix is still uncommitted.
- Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

Inputs and conditions the per-task tests do not make obvious; each has a pinning test in the task named:

1. Cancel or Stop pressed straight after `start_obd` returns, before the thread has sent anything (Task 6: `test_cancel_straight_after_start_records_nothing_and_frees_the_lock`).
2. Stop arriving as the first `0100` is answered, and Stop and Cancel both set (Task 3: `test_stop_before_the_pcm_answers_acts_as_a_cancel`, `test_cancel_wins_over_stop`).
3. A cancel after the read finished must not undo it (Task 5: `test_a_cancel_after_the_read_finished_does_not_undo_it`).
4. A stopped read after an earlier read of the same case must not make earlier codes or the freeze frame look gone (Task 4: `test_earlier_codes_and_freeze_frame_survive_a_read_stopped_before_them`).
5. A second Read pressed from another device or tab while one runs gets "already running" (Task 6: `test_a_second_start_while_a_read_runs_is_refused`).
6. `SerialTransport.open` cancelled right after connecting must close the port (Task 1: `test_cancel_during_open_closes_the_port`).
7. Switching session mid-read must not send the result to the other case (Task 6: `test_the_read_goes_to_the_case_it_was_started_on`).
8. The server stopping mid-read: the thread is a daemon and the OS closes the port. Not testable here; recorded in the final message.

---

### Task 1: ObdCancelled and cancellable serial transport

**Files:**
- Modify: `crank/obd/errors.py` (add `ObdCancelled` after `ObdTimeout`)
- Modify: `crank/obd/transport.py` (import it; add `cancel` and `check_cancel` to `Transport`)
- Modify: `crank/obd/serial_transport.py` (check in `send`; close the port when `open` is cancelled)
- Test: `tests/test_obd_serial.py`

**Interfaces:**
- Produces: `ObdCancelled(ObdError)`, `Transport.cancel = None` (any object with `is_set()`), `Transport.check_cancel() -> None` (raises `ObdCancelled` when `cancel.is_set()`). Later tasks set `transport.cancel`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_obd_serial.py`; add `import threading` and `import time` at the top, and `ObdCancelled` to the existing `crank.obd.errors` import)

```python
def test_cancel_interrupts_the_wait_for_a_reply():
    f = FakeSerial({"0100": [b"SEARCHING..."]})            # no prompt ever arrives
    t = make(f); t.open()
    t.cancel = threading.Event()
    threading.Timer(0.05, t.cancel.set).start()
    start = time.monotonic()
    with pytest.raises(ObdCancelled):
        t.send("0100", 20.0)
    assert time.monotonic() - start < 1.0


def test_a_set_cancel_stops_before_anything_is_written():
    f = FakeSerial()
    t = make(f); t.open()
    written = len(f.written)
    t.cancel = threading.Event(); t.cancel.set()
    with pytest.raises(ObdCancelled):
        t.send("0100", 1.0)
    assert len(f.written) == written


def test_cancel_during_open_closes_the_port():
    f = FakeSerial()
    t = make(f)
    t.cancel = threading.Event(); t.cancel.set()
    with pytest.raises(ObdCancelled):
        t.open()
    assert f.closed


def test_a_transport_without_a_cancel_event_sends_as_before():
    f = FakeSerial({"010C": [b"41 0C 0B B8\r\r>"]})
    t = make(f); t.open()
    assert t.cancel is None and t.send("010C", 1.0).strip() == "41 0C 0B B8"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_obd_serial.py -q -p no:cacheprovider`
Expected: collection error `ImportError: cannot import name 'ObdCancelled'`.

- [ ] **Step 3: Implement**

In `crank/obd/errors.py`, after `ObdTimeout`:

```python
class ObdCancelled(ObdError):
    """The person cancelled the read. Nothing is recorded."""

    def __init__(self, message: str = "Read cancelled; nothing was recorded."):
        super().__init__(message)
```

In `crank/obd/transport.py`: change the import to `from .errors import ObdCancelled, ObdNoAdapter, ObdTimeout` and make the top of `Transport` read:

```python
class Transport:
    """One command in, the adapter's reply text out (everything up to the '>' prompt)."""

    sim = False
    cancel = None                                      # anything with is_set(); polled while waiting for a reply

    def check_cancel(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise ObdCancelled()

    def open(self) -> None:
        raise NotImplementedError
```
(leave `send` and `close` as they are).

In `crank/obd/serial_transport.py`: add `ObdCancelled` to the `.errors` import. In `open`, change the `try` around `_atz` to:

```python
            try:
                self._atz()
                self.baud = baud
                return
            except ObdCancelled:
                self.close()
                raise
            except ObdTimeout:
                self.close()
                if i == len(bauds) - 1:
                    raise
```
In `send`, after the `if self._ser is None:` check add `self.check_cancel()`, and start the read loop with a check:

```python
                while b">" not in buf:
                    self.check_cancel()
                    if time.monotonic() > deadline:
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_obd_serial.py -q -p no:cacheprovider`
Expected: all pass (existing and the four new).

- [ ] **Step 5: Commit**

```bash
git add crank/obd/errors.py crank/obd/transport.py crank/obd/serial_transport.py tests/test_obd_serial.py
git commit -m "Let the serial transport be cancelled mid-wait

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Cancellable reader and the slow_adapter scenario

**Files:**
- Modify: `crank/obd/reader.py` (constructor, `_ask`, retry pause, `read()`)
- Modify: `crank/obd/transport.py` (`delay` field, `_wait`, `slow_adapter`)
- Modify: `tests/test_obd_allowlist.py:35` (skip delayed scenarios in the drift test)
- Test: `tests/test_obd_reader.py`, `tests/test_obd_transport.py`

**Interfaces:**
- Consumes: `ObdCancelled`, `Transport.cancel`, `Transport.check_cancel` (Task 1).
- Produces: `ObdReader(transport, ..., cancel=None)`; with `cancel` the reader sets `transport.cancel = cancel`, raises `ObdCancelled` before connecting, before each command and during the retry pause, and closes the transport on the way out. Methods `_check_interrupt()` and `_pause(seconds)` (Task 3 extends both). `Scenario.delay: float = 0.0`; scenario `slow_adapter`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_obd_reader.py` (add `import threading`, `import time`, and `ObdCancelled` to its errors import):

```python
def test_a_cancel_set_before_the_read_connects_to_nothing():
    ev = threading.Event(); ev.set()
    t = sent_by(SimTransport("healthy"))
    with pytest.raises(ObdCancelled):
        ObdReader(t, cancel=ev, retry_pause=0).read()
    assert t.sent == [] and not t.opened


def test_a_cancel_in_the_middle_stops_the_read_and_closes_the_transport():
    ev, closed = threading.Event(), []
    t = sent_by(SimTransport("healthy"))
    inner = t.send

    def send(cmd, timeout=5.0):
        if cmd == "010C":
            ev.set()
        return inner(cmd, timeout)
    t.send, t.close = send, lambda: closed.append(1)
    with pytest.raises(ObdCancelled):
        ObdReader(t, cancel=ev, retry_pause=0).read()
    assert closed and "010C" in t.sent and "0142" not in t.sent      # 0142 is asked right after 010C


def test_a_cancel_ends_the_pause_before_a_retry():
    ev = threading.Event()
    threading.Timer(0.1, ev.set).start()
    start = time.monotonic()
    with pytest.raises(ObdCancelled):
        ObdReader(SimTransport("transient_can_error"), cancel=ev, retry_pause=30).read()
    assert time.monotonic() - start < 5.0


def test_a_reader_without_a_cancel_event_still_reads():
    assert ObdReader(SimTransport("healthy"), retry_pause=0).read().live
```

Append to `tests/test_obd_transport.py` (add `import threading`, `import time`, `import pytest` and `ObdCancelled` if missing):

```python
def test_slow_adapter_delays_each_reply_and_a_cancel_cuts_the_wait():
    assert "slow_adapter" in SCENARIOS
    t = SimTransport("slow_adapter"); t.open()
    t.cancel = threading.Event()
    threading.Timer(0.1, t.cancel.set).start()
    start = time.monotonic()
    with pytest.raises(ObdCancelled):
        t.send("ATZ")
    assert time.monotonic() - start < 0.8


def test_a_scenario_without_a_delay_answers_at_once():
    t = SimTransport("healthy"); t.open()
    start = time.monotonic()
    t.send("ATZ")
    assert time.monotonic() - start < 0.5
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_obd_reader.py tests/test_obd_transport.py -q -p no:cacheprovider`
Expected: failures: `TypeError: ObdReader.__init__() got an unexpected keyword argument 'cancel'` and an assertion for `slow_adapter`.

- [ ] **Step 3: Implement**

`crank/obd/reader.py`: add `ObdCancelled` to the `.errors` import; add `cancel=None` as the constructor's last parameter and store `self.cancel = cancel`; replace `_ask` with:

```python
    def _check_interrupt(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise ObdCancelled()

    def _pause(self, seconds: float) -> None:
        if self.cancel is None:
            time.sleep(seconds)
        elif self.cancel.wait(seconds):
            raise ObdCancelled()

    def _ask(self, cmd: str, timeout: float | None = None) -> str:
        self._check_interrupt()
        resp = self.t.send(cmd, timeout or self.timeout)
        self.raw[cmd] = resp.strip()
        return resp
```
In `_request` replace `time.sleep(self.retry_pause)` with `self._pause(self.retry_pause)`. In `read()`, just before `self.t.open()` add:

```python
        self._check_interrupt()
        if self.cancel is not None:
            self.t.cancel = self.cancel
```

`crank/obd/transport.py`: add `import time` at the top; add to `Scenario` (after `search_delay`):

```python
    delay: float = 0.0                                 # seconds every reply takes (to try Cancel without hardware)
```
Add to `SCENARIOS` (after `"slow_search"`):

```python
    "slow_adapter": Scenario(values=LIVE_RUNNING, delay=1.0),
```
In `SimTransport`, add:

```python
    def _wait(self, seconds: float) -> None:
        """Sleep `seconds`, but give up at once when an interrupt arrives."""
        end = time.monotonic() + seconds
        while True:
            self.check_cancel()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.05, left))
```
and in `send`, right after `self.n += 1`, add `self._wait(self.sc.delay)`.

`tests/test_obd_allowlist.py:35`: skip delayed scenarios (their commands are `healthy`'s):

```python
@pytest.mark.parametrize("name", [n for n, s in SCENARIOS.items()
                                  if (not s.fault or s.fault.startswith("drop")) and not s.delay])
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_obd_reader.py tests/test_obd_transport.py tests/test_obd_allowlist.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add crank/obd/reader.py crank/obd/transport.py tests/test_obd_allowlist.py tests/test_obd_reader.py tests/test_obd_transport.py
git commit -m "Make the reader cancellable and add the slow_adapter scenario

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Stop in the reader, and the partial readout

**Files:**
- Modify: `crank/obd/errors.py` (add `ObdStopped`)
- Modify: `crank/obd/reasons.py` (reason `stopped`)
- Modify: `crank/obd/reader.py` (`_Interrupt`, `stop`, sections, `_finish_stopped`, `ObdReadout` fields, lines, event data)
- Test: `tests/test_obd_reader.py`, `tests/test_obd_reasons.py`

**Interfaces:**
- Consumes: `ObdCancelled`, `_check_interrupt`, `_pause`, `Transport.cancel` (Tasks 1 and 2).
- Produces: `ObdStopped(ObdError)`; `ObdReader(transport, ..., cancel=None, stop=None)`; `ObdReader.has_data: bool`; `ObdReadout.stopped: bool = False`, `ObdReadout.not_read: list = []` (strings from `"03" "07" "0A" "02"`); `LiveValue.reason == "stopped"`; `REASON_TEXT["stopped"] == "not read, stopped by you"`; event data keys `stopped` and `not_read`. A stop seen before the PCM has answered raises `ObdCancelled`; after, `read()` returns a readout with `stopped=True`.

- [ ] **Step 1: Write the failing tests**

Edit `tests/test_obd_reasons.py`: change the key-set assertion to `{"not_in_map", "refused", "busy", "no_answer", "stopped"}` and append:

```python
def test_describe_unavailable_puts_stopped_after_the_worrying_reasons():
    items = [{"name": "C", "reason": "stopped"}, {"name": "A", "reason": "no_answer"},
             {"name": "D", "reason": "not_in_map"}]
    assert describe_unavailable(items) == ("no answer: A; not read, stopped by you: C; "
                                           "not in the PCM's support map: D")
```

Append to `tests/test_obd_reader.py` (add `ObdStopped`-free imports: only `LIVE_PIDS` is already imported):

```python
def stop_read(scenario, after, **kw):
    """Read `scenario`, pressing Stop as soon as `after` has been answered."""
    stop = threading.Event()
    t = sent_by(SimTransport(scenario))
    inner = t.send

    def send(cmd, timeout=5.0):
        r = inner(cmd, timeout)
        if cmd == after:
            stop.set()
        return r
    t.send = send
    return ObdReader(t, stop=stop, retry_pause=0, **kw).read(), t


def test_stop_before_the_pcm_answers_acts_as_a_cancel():
    stop = threading.Event(); stop.set()
    t = sent_by(SimTransport("healthy"))
    with pytest.raises(ObdCancelled):
        ObdReader(t, stop=stop, retry_pause=0).read()
    assert t.sent == []


def test_stop_during_the_adapter_identification_acts_as_a_cancel():
    stop = threading.Event()
    t = sent_by(SimTransport("healthy"))
    inner = t.send

    def send(cmd, timeout=5.0):
        if cmd == "STI":
            stop.set()
        return inner(cmd, timeout)
    t.send = send
    with pytest.raises(ObdCancelled):
        ObdReader(t, stop=stop, retry_pause=0).read()


def test_cancel_wins_over_stop():
    cancel, stop = threading.Event(), threading.Event()
    cancel.set(); stop.set()
    with pytest.raises(ObdCancelled):
        ObdReader(SimTransport("healthy"), cancel=cancel, stop=stop, retry_pause=0).read()


def test_stop_right_after_the_first_answer_keeps_nothing_but_says_so():
    r, t = stop_read("many_codes", "0100")
    assert r.stopped and r.codes == [] and r.mil_on is None and r.freeze_frame is None
    assert r.not_read == ["03", "07", "0A", "02"]
    assert len(r.live) == len(LIVE_PIDS) and all(v.reason == "stopped" and not v.supported for v in r.live)
    assert "Stopped by you before the read finished." in r.notes
    assert "03" not in t.sent


def test_stop_after_mode_03_keeps_the_stored_codes_and_not_reads_the_rest():
    r, t = stop_read("many_codes", "03")
    assert r.stopped and {c["status"] for c in r.codes} == {"stored"} and len(r.codes) == 3
    assert r.not_read == ["07", "0A", "02"] and "07" not in t.sent


def test_stop_in_the_live_values_keeps_the_values_already_read():
    r, t = stop_read("healthy", "010C")
    assert r.stopped and r.not_read == [] and r.live[0].supported and r.live[0].name == "Engine RPM"
    assert all(v.reason == "stopped" for v in r.live[1:]) and len(r.live) == len(LIVE_PIDS)
    assert "0142" not in t.sent


def test_stop_in_the_freeze_frame_drops_the_partial_frame():
    r, _ = stop_read("p2119_zero_rpm", "020200")
    assert r.stopped and r.freeze_frame is None and r.not_read == ["02"]
    assert [c["code"] for c in r.codes] == ["P2119"]


def test_a_stopped_read_closes_the_transport():
    closed = []
    stop = threading.Event()
    t = sent_by(SimTransport("healthy"))
    inner = t.send

    def send(cmd, timeout=5.0):
        if cmd == "0100":
            stop.set()
        return inner(cmd, timeout)
    t.send, t.close = send, lambda: closed.append(1)
    assert ObdReader(t, stop=stop, retry_pause=0).read().stopped and closed


def test_a_stopped_read_says_what_was_not_read():
    r, _ = stop_read("many_codes", "0100")
    lines = r.lines()
    assert lines[0].endswith(": not read")
    assert any(l.startswith(f"Live values read: 0 of {len(LIVE_PIDS)}") and "not read, stopped by you" in l
               for l in lines)
    assert "Freeze frame not read" in lines and "Stopped by you before the read finished." in lines


def test_a_partly_read_set_of_codes_names_the_modes_not_read():
    r, _ = stop_read("many_codes", "03")
    assert "(modes not read: 07, 0A)" in r.lines()[0] and "P0335 (stored" in r.lines()[0]


def test_event_data_carries_stopped_and_not_read():
    r, _ = stop_read("many_codes", "03")
    d = r.to_event_data()
    assert d["stopped"] is True and d["not_read"] == ["07", "0A", "02"]
    full = read("healthy").to_event_data()
    assert full["stopped"] is False and full["not_read"] == []
```
(`"020200"` is the first freeze-frame request the reader sends, the frame's trouble code.)

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_obd_reader.py tests/test_obd_reasons.py -q -p no:cacheprovider`
Expected: failures: `TypeError: ... unexpected keyword argument 'stop'`, and the reasons assertions.

- [ ] **Step 3: Implement**

`crank/obd/errors.py`, after `ObdCancelled`:

```python
class ObdStopped(ObdError):
    """The person pressed Stop and the read has data worth keeping. Raised and caught inside the reader."""
```

`crank/obd/reasons.py`: add `"stopped": "not read, stopped by you",` to `REASON_TEXT` and change `_ORDER` to `("refused", "busy", "no_answer", "stopped", "not_in_map")`.

`crank/obd/reader.py`:
- import `ObdStopped` from `.errors`; add `CODE_MODES = ("03", "07", "0A")` and `UNREAD_ORDER = ("03", "07", "0A", "02")` below `MAX_RETRIES`.
- update the `LiveValue.reason` comment to `# "" = read; else not_in_map | refused | busy | no_answer | stopped`.
- add to `ObdReadout` (after `read_at`): `stopped: bool = False` and `not_read: list = field(default_factory=list)`.
- in `ObdReadout.lines()` replace the codes block:

```python
        unread = [m for m in self.not_read if m in CODE_MODES]
        if not codes:
            if len(unread) == len(CODE_MODES):
                codes = "not read"
            else:
                codes = ("no codes reported by the modes that were read" if (self.unsupported_modes or unread)
                         else "none stored, pending or permanent")
        if unread and len(unread) < len(CODE_MODES):
            codes += f" (modes not read: {', '.join(unread)})"
```
  and after the `if self.freeze_frame:` line add `elif "02" in self.not_read: out.append("Freeze frame not read")`.
- in `to_event_data()` add `"stopped": self.stopped, "not_read": self.not_read,`.
- add above `ObdReader`:

```python
class _Interrupt:
    """What the transport polls while it waits: a cancel, or a stop before the PCM has answered anything
    (there is nothing to keep then, so a stop acts as a cancel)."""

    def __init__(self, reader: "ObdReader"):
        self.reader = reader

    def is_set(self) -> bool:
        r = self.reader
        return bool((r.cancel is not None and r.cancel.is_set())
                    or (r.stop is not None and r.stop.is_set() and not r.has_data))

    def wait(self, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while True:
            if self.is_set():
                return True
            left = end - time.monotonic()
            if left <= 0:
                return False
            time.sleep(min(0.02, left))
```
- constructor: add `stop=None` after `cancel=None`; set `self.stop = stop`, `self.has_data = False`, `self._section = ""`, `self._interrupt = _Interrupt(self)`.
- replace `_check_interrupt` and `_pause`:

```python
    def _check_interrupt(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise ObdCancelled()
        if self.stop is not None and self.stop.is_set():
            if not self.has_data:
                raise ObdCancelled()                      # nothing to keep yet
            raise ObdStopped()

    def _pause(self, seconds: float) -> None:
        if self.cancel is None and self.stop is None:
            time.sleep(seconds)
        elif self._interrupt.wait(seconds):
            raise ObdCancelled()
```
- in `read()`: reset `self.has_data, self._section = False, ""` with the other resets; replace the wiring added in Task 2 by

```python
        self._check_interrupt()
        if self.cancel is not None or self.stop is not None:
            self.t.cancel = self._interrupt
```
  right after the `msgs is None` check add `self.has_data, self._section = True, "maps"`; add, before the existing `except BaseException:` clause, `except ObdStopped:` with body `self._finish_stopped(out)` (the read then falls through to the normal `self.t.close()` path).
- set the section at the start of each phase: `self._section = "status"` first line of `_status`; in `_codes`, `self._section = f"{mode:02X}"` as the first line inside the `for status, mode in STATUS_MODES:` loop; `self._section = "02"` first line of `_freeze`; `self._section = "live"` first line of `_live`.
- add:

```python
    def _finish_stopped(self, out: ObdReadout) -> None:
        """Pressing Stop: keep the sections that finished, drop the one in progress, and say what was not read."""
        sec = self._section
        if sec in UNREAD_ORDER:
            out.not_read = list(UNREAD_ORDER[UNREAD_ORDER.index(sec):])
        elif sec in ("maps", "status"):
            out.not_read = list(UNREAD_ORDER)
        have = {v.pid for v in out.live}
        for pid in LIVE_PIDS:
            if pid not in have:
                out.live.append(LiveValue(pid, PIDS[pid].name, PIDS[pid].unit, None, "stopped"))
        out.stopped = True
        out.notes.append("Stopped by you before the read finished.")
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_obd_reader.py tests/test_obd_reasons.py tests/test_obd_transport.py tests/test_obd_serial.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add crank/obd/errors.py crank/obd/reasons.py crank/obd/reader.py tests/test_obd_reader.py tests/test_obd_reasons.py
git commit -m "Let a read be stopped and keep what was read

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Present a stopped read in the case summary and reports

**Files:**
- Modify: `crank/summary.py` (timeline line; the unread set)
- Test: `tests/test_obd_case.py`

**Interfaces:**
- Consumes: `ObdReadout.stopped`, `not_read`, event keys `stopped` / `not_read`, reason `stopped` (Task 3). `render.py` already prints `obd_unavailable` and `obd_notes`, and `REASON_TEXT` now has `stopped`, so markdown and HTML need no change.
- Produces: the `code_read` timeline line says "codes not read" when no code mode was read, lists "modes not read: …" including `not_read`, and ends with "; stopped by you before it finished"; earlier codes and the earlier freeze frame survive a mode the latest read did not read.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_obd_case.py`; add `import threading` at the top)

```python
def stopped_read(scenario, after):
    stop = threading.Event()
    t = SimTransport(scenario)
    inner = t.send

    def send(cmd, timeout=5.0):
        r = inner(cmd, timeout)
        if cmd == after:
            stop.set()
        return r
    t.send = send
    return ObdReader(t, stop=stop, retry_pause=0).read()


def test_a_stopped_read_is_recorded_with_what_was_not_read(case):
    events = case.append_obd(stopped_read("many_codes", "0100"))
    d = events[0].data
    assert d["stopped"] is True and d["not_read"] == ["03", "07", "0A", "02"]
    assert not [e for e in events if e.type == "measurement"]


def test_a_stopped_read_does_not_say_there_were_no_codes(case):
    case.append_obd(stopped_read("many_codes", "0100"))
    line = [t["line"] for t in build_summary(case).timeline if t["type"] == "code_read"][-1]
    assert "codes not read" in line and "stopped by you before it finished" in line
    assert "no stored, pending or permanent codes" not in line


def test_a_partly_read_set_of_codes_lists_the_modes_not_read(case):
    case.append_obd(stopped_read("many_codes", "03"))
    s = build_summary(case)
    line = [t["line"] for t in s.timeline if t["type"] == "code_read"][-1]
    assert "P0335 (stored)" in line and "modes not read: 07, 0A, 02" in line
    assert any(c["code"] == "P0335" and c["status"] == "stored" for c in s.codes)


def test_earlier_codes_and_freeze_frame_survive_a_read_stopped_before_them(case):
    case.append_obd(read("many_codes"))
    case.append_obd(stopped_read("healthy", "0100"))
    s = build_summary(case)
    assert any(c["code"] == "P0335" and c["status"] == "stored" for c in s.codes)
    assert not any(c["status"] == "not in latest OBD read" for c in s.codes)
    assert s.freeze_frame and s.freeze_frame["code"] == "P0335"


def test_values_not_reached_are_listed_as_not_read_in_both_reports(case):
    case.append_obd(stopped_read("healthy", "010C"))
    s = build_summary(case)
    assert s.obd_unavailable and all(u["reason"] == "stopped" for u in s.obd_unavailable)
    for out in (to_markdown(s), to_html(s)):
        assert "not read, stopped by you" in out and "Stopped by you before the read finished." in out


def test_a_read_that_was_not_stopped_looks_as_before(case):
    case.append_obd(read("healthy"))
    line = [t["line"] for t in build_summary(case).timeline if t["type"] == "code_read"][-1]
    assert "stopped" not in line and "modes not read" not in line


def test_old_events_without_stopped_or_not_read_still_render(case):
    case.append("code_read", "", {"codes": [], "source": "obd", "sim": True})
    s = build_summary(case)
    assert [t for t in s.timeline if t["type"] == "code_read"] and to_markdown(s) and to_html(s)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_obd_case.py -q -p no:cacheprovider`
Expected: the new stopped-read tests fail (`codes not read` missing; earlier codes demoted to "not in latest OBD read").

- [ ] **Step 3: Implement** (`crank/summary.py`)

In `describe`, in the `d.get("source") == "obd"` branch replace the lines from `unread = ...` to the `return` with:

```python
            not_read = [str(m) for m in d.get("not_read") or []]
            unread = [str(m) for m in d.get("unsupported_modes") or []] + not_read
            if not codes:
                if {"03", "07", "0A"} <= set(not_read):
                    codes = "codes not read"
                else:
                    codes = ("no codes reported by the modes that were read" if unread
                             else "no stored, pending or permanent codes")
            return (f"OBD read{sim}: {codes}" + (f"; modes not read: {', '.join(unread)}" if unread else "")
                    + ("; stopped by you before it finished" if d.get("stopped") else ""))
```
In `build_summary`, change `unread = set(e.data.get("unsupported_modes") or [])` to:

```python
            unread = set(e.data.get("unsupported_modes") or []) | set(e.data.get("not_read") or [])
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_obd_case.py tests/test_summary.py tests/test_report.py tests/test_render.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add crank/summary.py tests/test_obd_case.py
git commit -m "Show what a stopped OBD read did not read

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: ObdJob

**Files:**
- Create: `crank/obd/job.py`
- Test: `tests/test_obd_job.py`

**Interfaces:**
- Consumes: `ObdCancelled`, `ObdError` (Task 1); `ObdReadout.stopped` (Task 3, read with `getattr`).
- Produces: `ObdJob(run, session_name="", on_finish=None)` where `run(cancel_event, stop_event) -> readout`. Attributes `state` (`idle|running|done|stopped|error|cancelled`), `message`, `readout`, `session_name`, `cancel_event`, `stop_event`, `started_at`; methods `start()`, `cancel()`, `stop()`, `join(timeout=None)`; properties `running`, `seconds`. `on_finish()` runs before the final state becomes visible.

- [ ] **Step 1: Write the failing tests** (`tests/test_obd_job.py`)

```python
import threading
from types import SimpleNamespace

from crank.obd.errors import ObdCancelled, ObdProtocolError
from crank.obd.job import ObdJob


def job_of(run, **kw):
    j = ObdJob(run, **kw)
    j.start()
    return j


def test_a_finished_read_is_done_with_its_readout():
    j = job_of(lambda c, s: "R")
    j.join(5)
    assert j.state == "done" and j.readout == "R" and not j.running and j.message == ""


def test_a_readout_flagged_stopped_makes_the_job_stopped():
    r = SimpleNamespace(stopped=True)
    j = job_of(lambda c, s: r); j.join(5)
    assert j.state == "stopped" and j.readout is r and not j.running


def test_stop_sets_the_stop_event_and_cancel_sets_the_cancel_event():
    j = ObdJob(lambda c, s: "R")
    assert not j.stop_event.is_set() and not j.cancel_event.is_set()
    j.stop(); assert j.stop_event.is_set() and not j.cancel_event.is_set()
    j.cancel(); assert j.cancel_event.is_set()


def test_cancel_ends_a_waiting_read_as_cancelled():
    def run(c, s):
        c.wait(5)
        raise ObdCancelled()
    j = job_of(run)
    assert j.running
    j.cancel(); j.join(5)
    assert j.state == "cancelled" and j.readout is None and "nothing was recorded" in j.message


def test_an_obd_error_is_kept_as_the_message():
    def run(c, s):
        raise ObdProtocolError("The vehicle did not answer. Is the key on?")
    j = job_of(run); j.join(5)
    assert j.state == "error" and j.message == "The vehicle did not answer. Is the key on?"


def test_an_unexpected_error_names_its_type():
    def run(c, s):
        raise ValueError("boom")
    j = job_of(run); j.join(5)
    assert j.state == "error" and j.message == "ValueError: boom"


def test_a_cancel_after_the_read_finished_does_not_undo_it():
    def run(c, s):
        c.set()                                            # the cancel lands as the read completes
        return "R"
    j = job_of(run); j.join(5)
    assert j.state == "done" and j.readout == "R"


def test_on_finish_runs_before_the_final_state_is_visible():
    seen, holder = [], {}
    j = ObdJob(lambda c, s: "R", on_finish=lambda: seen.append(holder["j"].state))
    holder["j"] = j
    j.start(); j.join(5)
    assert seen == ["running"] and j.state == "done"


def test_seconds_counts_while_running_and_stops_when_finished():
    gate = threading.Event()
    j = job_of(lambda c, s: gate.wait(5) and "R")
    assert j.running and j.seconds >= 0
    gate.set(); j.join(5)
    done_at = j.seconds
    assert j.seconds == done_at


def test_a_job_that_was_never_started_is_idle():
    j = ObdJob(lambda c, s: "R")
    assert j.state == "idle" and not j.running and j.seconds == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_obd_job.py -q -p no:cacheprovider`
Expected: `ModuleNotFoundError: No module named 'crank.obd.job'`.

- [ ] **Step 3: Implement** (`crank/obd/job.py`)

```python
"""ObdJob: one OBD read in a background thread, so the UI can show it running, stop it or cancel it."""
from __future__ import annotations

import threading
import time
from typing import Callable

from .errors import ObdCancelled, ObdError


class ObdJob:
    def __init__(self, run: Callable[[threading.Event, threading.Event], object], session_name: str = "",
                 on_finish: Callable[[], None] | None = None):
        self._run, self.session_name, self._on_finish = run, session_name, on_finish
        self.cancel_event, self.stop_event = threading.Event(), threading.Event()
        self.state, self.message, self.readout = "idle", "", None
        self.started_at = self.finished_at = 0.0
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.started_at = time.time()
        self.state = "running"
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        """Throw the read away: nothing is recorded."""
        self.cancel_event.set()

    def stop(self) -> None:
        """End the read and keep what was read (before the PCM has answered, this acts as cancel)."""
        self.stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def seconds(self) -> float:
        if not self.started_at:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    def _work(self) -> None:
        state, message, readout = "error", "", None
        try:
            readout = self._run(self.cancel_event, self.stop_event)
            state = "stopped" if getattr(readout, "stopped", False) else "done"
        except ObdCancelled as e:
            state, message = "cancelled", str(e)
        except ObdError as e:
            message = str(e)
        except Exception as e:                        # a storage or parsing bug: nothing was recorded
            message = f"{type(e).__name__}: {e}"
        finally:
            if self._on_finish:
                self._on_finish()                     # free the controller's lock before anyone sees the end
            self.readout, self.message, self.finished_at = readout, message, time.time()
            self.state = state
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_obd_job.py -q -p no:cacheprovider`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add crank/obd/job.py tests/test_obd_job.py
git commit -m "Add ObdJob to run an OBD read in a thread

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Controller start_obd, cancel_obd, stop_obd, obd_job

**Files:**
- Modify: `crank/controller.py` (imports, `__init__`, new methods, `read_obd` becomes a wrapper)
- Test: `tests/test_obd_controller.py`

**Interfaces:**
- Consumes: `ObdJob` (Task 5), `ObdReader(..., cancel=, stop=)` (Tasks 2 and 3), `slow_adapter` (Task 2).
- Produces: `Controller.start_obd(source) -> str | None` (message, or `None` once the job runs), `cancel_obd()`, `stop_obd()`, `obd_job: ObdJob | None`; `read_obd(source) -> (readout, None) | (None, message)` unchanged.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_obd_controller.py`; add `import time` and `from crank.case import Case` and `from crank.obd.errors import ObdNoAdapter` and `from crank.obd.transport import SimTransport` if missing)

```python
class Gated(SimTransport):
    """A simulated adapter whose replies wait for `gate` (or a cancel) once `after` commands have been sent."""

    def __init__(self, scenario, gate, after=0):
        super().__init__(scenario)
        self.gate, self.after, self.entered = gate, after, threading.Event()

    def send(self, cmd, timeout=5.0):
        if self.n >= self.after:
            self.entered.set()
            while not self.gate.is_set():
                self.check_cancel()
                time.sleep(0.01)
        return super().send(cmd, timeout)


def test_start_obd_runs_the_read_in_the_background_and_records_it(tmp_path):
    c = Controller(SessionStore(tmp_path))
    assert c.start_obd("p2119_zero_rpm") is None
    c.obd_job.join(10)
    assert c.obd_job.state == "done" and c.obd_job.readout.codes[0]["code"] == "P2119"
    assert [e.type for e in c.case().events()].count("code_read") == 1


def test_a_second_start_while_a_read_runs_is_refused(tmp_path):
    c = Controller(SessionStore(tmp_path))
    assert c.start_obd("slow_adapter") is None
    assert c.start_obd("healthy") == "An OBD read is already running."
    first = c.obd_job
    c.cancel_obd(); first.join(5)
    assert c.obd_job is first


def test_cancel_straight_after_start_records_nothing_and_frees_the_lock(tmp_path):
    c = Controller(SessionStore(tmp_path))
    assert c.start_obd("slow_adapter") is None
    c.cancel_obd()
    c.obd_job.join(5)
    assert c.obd_job.state == "cancelled" and c.case().events() == []
    assert c.start_obd("healthy") is None                  # the next read reopens cleanly
    c.obd_job.join(10)
    assert c.obd_job.state == "done"


def test_stop_after_the_pcm_answered_keeps_what_was_read(tmp_path):
    c = Controller(SessionStore(tmp_path))
    gate = threading.Event()
    t = Gated("many_codes", gate, after=11)               # 6 init + 4 id + 0100 answered, then it waits
    c._obd_transport = lambda source: t
    assert c.start_obd("x") is None
    assert t.entered.wait(5)
    c.stop_obd()
    gate.set()
    c.obd_job.join(10)
    assert c.obd_job.state == "stopped" and c.obd_job.readout.stopped
    events = [e for e in c.case().events() if e.type == "code_read"]
    assert len(events) == 1 and events[0].data["stopped"] is True


def test_stop_before_the_pcm_answers_acts_as_a_cancel(tmp_path):
    c = Controller(SessionStore(tmp_path))
    t = Gated("healthy", threading.Event(), after=0)       # never answers
    c._obd_transport = lambda source: t
    assert c.start_obd("x") is None
    assert t.entered.wait(5)
    c.stop_obd()
    c.obd_job.join(5)
    assert c.obd_job.state == "cancelled" and c.case().events() == []


def test_the_read_goes_to_the_case_it_was_started_on(tmp_path):
    c = Controller(SessionStore(tmp_path))
    ev = threading.Event()
    c._obd_transport = lambda source: Gated("healthy", ev)
    assert c.start_obd("x") is None
    c.session_name = "other"
    ev.set()
    c.obd_job.join(10)
    assert c.obd_job.state == "done"
    assert [e.type for e in Case(c.store.open("default")).events()].count("code_read") == 1
    assert Case(c.store.open("other")).events() == []


def test_start_obd_refuses_the_other_kind_at_once_and_leaves_the_lock_free(tmp_path):
    c = Controller(SessionStore(tmp_path))
    r, err = c.read_obd("healthy")                         # a simulated read is in the case
    assert err is None

    class RealLike(SimTransport):
        sim = False
    c._obd_transport = lambda source: RealLike("healthy")
    msg = c.start_obd("x")
    assert msg and "already has simulated" in msg
    c._obd_transport = lambda source: SimTransport("healthy")
    assert c.start_obd("x") is None
    c.obd_job.join(10)


def test_start_obd_returns_a_transport_failure_and_leaves_the_lock_free(tmp_path, monkeypatch):
    c = Controller(SessionStore(tmp_path))

    def no_port(value):
        raise ObdNoAdapter("No OBD adapter found.")
    monkeypatch.setattr("crank.controller.resolve_port", no_port)
    assert c.start_obd(("serial", "auto")) == "No OBD adapter found."
    assert c.start_obd("healthy") is None
    c.obd_job.join(10)


def test_a_failed_read_is_an_error_job_that_records_nothing(tmp_path):
    c = Controller(SessionStore(tmp_path))
    assert c.start_obd("silent_adapter") is None
    c.obd_job.join(10)
    assert c.obd_job.state == "error" and c.obd_job.message and c.case().events() == []

```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_obd_controller.py -q -p no:cacheprovider`
Expected: `AttributeError: 'Controller' object has no attribute 'start_obd'`.

- [ ] **Step 3: Implement** (`crank/controller.py`)

Add `from .obd.job import ObdJob` to the imports. In `__init__`, after `self._obd_lock = threading.Lock()` add `self.obd_job: ObdJob | None = None`. Replace the whole `read_obd` method with:

```python
    def start_obd(self, source="healthy") -> str | None:
        """Start one OBD read into the current case in the background. Returns a message if it could not
        start, else None. `source` is a simulator scenario name, or ("serial", port | "auto") for a real adapter.

        The lock is held until the job ends, so a second read from the phone or another tab is refused."""
        if not self._obd_lock.acquire(blocking=False):
            return "An OBD read is already running."
        started = False
        try:
            try:
                transport = self._obd_transport(source)
            except ObdError as e:
                return str(e)
            wrong = self._wrong_kind(transport)
            if wrong:
                return wrong
            case = self.case()                              # the read goes to the case it was started on

            def run(cancel, stop):
                readout = ObdReader(transport, cancel=cancel, stop=stop).read()
                case.append_obd(readout)                    # last step: a cancelled or failed read adds nothing
                return readout
            job = ObdJob(run, session_name=self.session_name, on_finish=self._obd_lock.release)
            self.obd_job = job
            job.start()
            started = True
            return None
        finally:
            if not started:
                self._obd_lock.release()

    def cancel_obd(self) -> None:
        """Throw the running read away; nothing is recorded."""
        job = self.obd_job
        if job and job.running:
            job.cancel()

    def stop_obd(self) -> None:
        """End the running read and keep what was read (acts as a cancel before the PCM has answered)."""
        job = self.obd_job
        if job and job.running:
            job.stop()

    def read_obd(self, source="healthy"):
        """One OBD read into the current case, waiting for it to finish.
        Returns (readout, None) or (None, message)."""
        err = self.start_obd(source)
        if err:
            return None, err
        job = self.obd_job
        job.join()
        return (job.readout, None) if job.state in ("done", "stopped") else (None, job.message)
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_obd_controller.py tests/test_controller.py -q -p no:cacheprovider`
Expected: all pass, including the existing `read_obd` tests.

- [ ] **Step 5: Commit**

```bash
git add crank/controller.py tests/test_obd_controller.py
git commit -m "Run OBD reads as a background job the controller can stop or cancel

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Running indicator, Stop, Cancel and result on both pages

**Files:**
- Modify: `case_ui.py` (new `obd_panel`; `_obd` uses it)
- Modify: `remote.py` (the Read OBD expander uses it)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `Controller.start_obd`, `cancel_obd`, `stop_obd`, `obd_job`, `ObdJob.running/state/message/readout/session_name/seconds/join` (Tasks 5 and 6).
- Produces: `case_ui.obd_panel(ctrl, source, key, label, full_width=False)`. Widget keys: the start button uses `key` (`obd_read` on the Case page, `remote_obd` on the phone); Stop uses `f"{key}_stop"`, Cancel `f"{key}_cancel"`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_app.py`; add `import threading` and `import time` at the top, and `from crank.obd.transport import SimTransport`)

```python
def _waiting_read(monkeypatch, after=0):
    """Make the next OBD read wait once `after` commands were sent. Returns the event that lets it go on.
    after=0 waits before the PCM has answered anything; after=11 waits right after the first 0100 answer."""
    gate = threading.Event()

    class Waiting(SimTransport):
        def send(self, cmd, timeout=5.0):
            if self.n >= after:
                while not gate.is_set():
                    self.check_cancel()
                    time.sleep(0.01)
            return super().send(cmd, timeout)
    monkeypatch.setattr(get_controller(), "_obd_transport", lambda source: Waiting("healthy"))
    return gate


def test_case_page_shows_a_running_read_and_cancel_records_nothing(at, monkeypatch):
    gate = _waiting_read(monkeypatch)
    try:
        at.button(key="obd_read").click().run()
        assert not at.exception
        assert any("Reading from the vehicle" in i.value for i in at.info)
        assert at.button(key="obd_read").disabled
        at.button(key="obd_read_cancel").click().run()
        assert any("Read cancelled; nothing was recorded." in w.value for w in at.warning)
        assert get_controller().case().events() == []
        assert not at.button(key="obd_read").disabled
    finally:
        gate.set()


def test_case_page_stop_keeps_what_was_read(at, monkeypatch):
    gate = _waiting_read(monkeypatch, after=11)
    try:
        at.button(key="obd_read").click().run()
        assert any("Reading from the vehicle" in i.value for i in at.info)
        threading.Timer(0.3, gate.set).start()             # the command in flight finishes, then it stops
        at.button(key="obd_read_stop").click().run()
        assert any("Stopped; kept what was read" in s.value for s in at.success)
        events = [e for e in get_controller().case().events() if e.type == "code_read"]
        assert len(events) == 1 and events[0].data["stopped"] is True
    finally:
        gate.set()


def test_stop_before_the_pcm_answers_cancels_on_the_case_page(at, monkeypatch):
    gate = _waiting_read(monkeypatch)
    try:
        at.button(key="obd_read").click().run()
        at.button(key="obd_read_stop").click().run()
        assert any("Read cancelled" in w.value for w in at.warning)
        assert get_controller().case().events() == []
    finally:
        gate.set()


def test_phone_remote_shows_a_running_read_with_both_buttons_and_cancel_stops_it(at, monkeypatch):
    gate = _waiting_read(monkeypatch)
    try:
        at.query_params["page"] = "remote"
        at.run()
        at.button(key="remote_obd").click().run()
        assert any("Reading from the vehicle" in i.value for i in at.info)
        assert at.button(key="remote_obd_stop") and at.button(key="remote_obd_cancel")
        at.button(key="remote_obd_cancel").click().run()
        assert any("Read cancelled" in w.value for w in at.warning)
        assert get_controller().case().events() == []
    finally:
        gate.set()


def test_a_finished_read_is_not_shown_on_another_session(at):
    at.selectbox(key="obd_scenario").select("p2119_zero_rpm").run()
    at.button(key="obd_read").click().run()
    assert any("Recorded in the case" in s.value for s in at.success)
    get_controller().store.open("other")
    at.run()
    at.sidebar.selectbox(key="session_pick").select("other").run()
    assert not at.exception
    assert not any("Recorded in the case" in s.value for s in at.success)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_app.py -q -p no:cacheprovider -k "running_read or stop_keeps or stop_before or another_session"`
Expected: the new tests fail (the button blocks and no info or buttons exist; `at.button(key="obd_read_cancel")` finds nothing).

- [ ] **Step 3: Implement**

In `case_ui.py` add `import os` and `import time` if not already imported, then add above `_obd`:

```python
def obd_panel(ctrl, source, key: str, label: str, full_width: bool = False) -> None:
    """The start button, the running indicator with Stop and Cancel, and the last read's result.

    The result shows only on the session the read was started on, and stays until the next read starts."""
    job = ctrl.obd_job
    running = bool(job and job.running)
    if st.button(label, key=key, use_container_width=full_width, disabled=source is None or running):
        err = ctrl.start_obd(source)
        if err:
            st.error(err)
            return
        job = ctrl.obd_job
        job.join(0.5)                                 # a quick read finishes before the page is drawn
    if job is None or job.session_name != ctrl.session_name:
        return
    if job.running:
        st.info(f"Reading from the vehicle… {job.seconds:.0f} s")
        c1, c2 = st.columns(2)
        if c1.button("Stop (keep what was read)", key=f"{key}_stop", use_container_width=True):
            ctrl.stop_obd()
            job.join(2.0)
        if c2.button("Cancel", key=f"{key}_cancel", use_container_width=True):
            ctrl.cancel_obd()
            job.join(2.0)
        if job.running:
            refresh = float(os.environ.get("CRANK_REMOTE_REFRESH_S", "1"))
            if refresh > 0:                           # tests set 0 to render once instead of polling
                time.sleep(refresh)
                st.rerun()
            return
    if job.state in ("done", "stopped"):
        st.success("Recorded in the case:" if job.state == "done"
                   else "Stopped; kept what was read. Recorded in the case:")
        for line in job.readout.lines():
            st.write("- " + line)
    elif job.state == "cancelled":
        st.warning(job.message)
    elif job.state == "error":
        st.error(job.message)
```

Replace the body of `_obd()` after `source = pick_obd_source("obd")` with:

```python
    obd_panel(get_controller(), source, "obd_read", "Read from OBD")
```

In `remote.py` replace the `if st.button("Read OBD", ...)` block inside the expander (through its last `st.write`) with:

```python
        from case_ui import obd_panel
        obd_panel(ctrl, source, "remote_obd", "Read OBD", full_width=True)
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_app.py -q -p no:cacheprovider`
Expected: all pass, including the existing `test_case_page_obd_read_records_codes_and_shows_a_result`, `test_case_page_obd_failure_shows_an_error_and_records_nothing` and `test_phone_remote_obd_button`.
If a Streamlit `AppTest` run times out under machine load, re-run that test alone before suspecting the change.

- [ ] **Step 5: Commit**

```bash
git add case_ui.py remote.py
git add -p tests/test_app.py        # take only this task's tests if the unrelated session fix is still uncommitted
git commit -m "Show a running OBD read with Stop and Cancel on the Case page and the phone

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Documentation

**Files:**
- Modify: `README.md` (scenario count and list; a short "Stopping or cancelling a read" paragraph)

**Interfaces:**
- Consumes: everything above (documents behaviour only).

- [ ] **Step 1: Check the current wording**

Run: `grep -n "twenty-nine\|Read from OBD\|slow_search" README.md`
Expected: the "twenty-nine scenarios" sentence (about line 105) and the **Read from OBD** paragraph (about line 99).

- [ ] **Step 2: Edit**

Change "twenty-nine scenarios" to "thirty scenarios" and add `slow_adapter` (every reply takes about a second, so you can try Stop and Cancel without hardware) to the scenario list in that sentence. After the paragraph that begins "**When the PCM does not give a value.**" add:

```markdown
**Stopping or cancelling a read.** A read runs in the background. While it runs, the Case page and the phone's Read OBD show "Reading from the vehicle… N s" with **Stop (keep what was read)** and **Cancel**, and the Read button is disabled; a second read from another device or tab is refused until it ends. **Cancel** stops the read within a fraction of a second, even during the protocol search, closes the adapter's port and records nothing: no event and no measurements, and the page says so. **Stop** ends the read at the next command and records what was read, marked as stopped: trouble-code modes and the freeze frame it did not reach are shown as "not read" (never as "none"), live values it did not reach are listed as "not read, stopped by you", and codes from an earlier read are not marked as gone. A Stop pressed before the vehicle has answered anything has nothing to keep and acts as Cancel. The simulator's `slow_adapter` scenario lets you try both without hardware. Not checked against a real adapter yet (#3).
```

- [ ] **Step 3: Run the full suite**

Run: `python -m pytest -q -p no:cacheprovider --ignore=tests/test_app.py` then `python -m pytest tests/test_app.py -q -p no:cacheprovider`
Expected: all pass (the app tests run separately because `AppTest` can time out under heavy machine load).

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "Document stopping and cancelling an OBD read

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```
