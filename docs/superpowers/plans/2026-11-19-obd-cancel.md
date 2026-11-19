# Cancelling an OBD Read Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A running OBD read can be cancelled from the laptop Case page or the phone remote within about 0.1 s, and a cancelled read records nothing.

**Architecture:** An `ObdCancelled` error and a `threading.Event` that the reader and the transports check. An `ObdJob` runs the read in a thread (like `CaptureJob`); the controller holds it and exposes `start_obd` / `cancel_obd` / `obd_job`, keeping `read_obd` as a blocking wrapper. One shared UI helper draws the button, the running indicator, Cancel and the result on both pages.

**Tech Stack:** Python 3, `threading`, pyserial (lazy), Streamlit (`AppTest` for UI tests), pytest. Run tests with `python -m pytest` (the system `python` has pyserial; `.venv` does not).

**Spec:** `docs/superpowers/specs/2026-11-19-obd-cancel-design.md`

## Global Constraints

- A cancelled read records nothing: no event, no measurements. The message is exactly `Read cancelled; nothing was recorded.`
- Progress display is "Reading from the vehicle… N s" plus Cancel only: no stage names, no counts.
- `ObdCancelled` is an `ObdError` but not an `ObdAdapterError`, so the reader never retries it.
- Cancel must land within about 0.1 s while `SerialTransport.send` waits for a reply; no new command is written once the event is set.
- Every existing caller of `Controller.read_obd` and every existing test keeps working unchanged (`read_obd` still returns `(readout, None)` or `(None, message)`).
- Simulator: 30 scenarios after this slice; the new one is `slow_adapter` (about 1 s per reply).
- Only allowlisted read-only commands are ever sent; this slice adds none.
- Commit only the files a task names. The working tree also holds an uncommitted, unrelated session-creation fix in `app.py` and `tests/test_app.py`: stage files by name and never `git add -A` or `git add .`. For `tests/test_app.py` use `git add -p` only if the unrelated hunk would be swept in; otherwise stage the whole file once the owner has committed that fix. (If it is still uncommitted when Task 5 commits, stage with `git add -p tests/test_app.py` and take only this task's hunks.)
- Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

Inputs and conditions the per-task tests do not make obvious; each has a pinning test in the task named:

1. Cancel pressed straight after `start_obd` returns, before the thread has sent anything (Task 4: `test_cancel_straight_after_start_records_nothing_and_frees_the_lock`).
2. A cancel that arrives after the read has finished must not turn a recorded read into a cancelled one (Task 3: `test_a_cancel_after_the_read_finished_does_not_undo_it`).
3. A second Read pressed from another device or tab while one runs gets "already running", not a second job (Task 4: `test_a_second_start_while_a_read_runs_is_refused`).
4. `SerialTransport.open` cancelled right after connecting must close the port (Task 1: `test_cancel_during_open_closes_the_port`).
5. A cancelled read followed at once by a new one must reopen cleanly (Task 4, same test as 1).
6. Switching session mid-read must not send the result to the other case (Task 4: `test_the_read_goes_to_the_case_it_was_started_on`).
7. The server stopping mid-read: the thread is a daemon and the OS closes the port. Not testable here; recorded in the final message.

---

### Task 1: ObdCancelled and cancellable serial transport

**Files:**
- Modify: `crank/obd/errors.py` (add `ObdCancelled` after `ObdTimeout`)
- Modify: `crank/obd/transport.py` (import it; add `cancel` and `check_cancel` to `Transport`)
- Modify: `crank/obd/serial_transport.py` (check in `send`; close the port when `open` is cancelled)
- Test: `tests/test_obd_serial.py`

**Interfaces:**
- Produces: `ObdCancelled(ObdError)`, `Transport.cancel: threading.Event | None = None`, `Transport.check_cancel() -> None` (raises `ObdCancelled` when the event is set). Later tasks set `transport.cancel = event`.

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

In `crank/obd/transport.py`: change the import to `from .errors import ObdCancelled, ObdNoAdapter, ObdTimeout` and replace the `Transport` class body's first lines so it reads:

```python
class Transport:
    """One command in, the adapter's reply text out (everything up to the '>' prompt)."""

    sim = False
    cancel = None                                      # a threading.Event the reader sets; checked while waiting

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
In `send`, after the `if self._ser is None:` check add `self.check_cancel()`, and make the read loop start with a check:

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
- Produces: `ObdReader(transport, ..., cancel=None)`; when `cancel` is given the reader sets `transport.cancel = cancel`, raises `ObdCancelled` before connecting, before each command and during the retry pause, and closes the transport on the way out. `Scenario.delay: float = 0.0`; scenario `slow_adapter`.

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
Expected: failures: `TypeError: ObdReader.__init__() got an unexpected keyword argument 'cancel'` and `KeyError`/`AssertionError` for `slow_adapter`.

- [ ] **Step 3: Implement**

`crank/obd/reader.py`: add `ObdCancelled` to the `.errors` import; change the constructor to take `cancel=None` as its last parameter and store `self.cancel = cancel`; replace `_ask` with:

```python
    def _check_cancel(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise ObdCancelled()

    def _pause(self, seconds: float) -> None:
        if self.cancel is None:
            time.sleep(seconds)
        elif self.cancel.wait(seconds):
            raise ObdCancelled()

    def _ask(self, cmd: str, timeout: float | None = None) -> str:
        self._check_cancel()
        resp = self.t.send(cmd, timeout or self.timeout)
        self.raw[cmd] = resp.strip()
        return resp
```
In `_request` replace `time.sleep(self.retry_pause)` with `self._pause(self.retry_pause)`. In `read()`, just before `self.t.open()` add:

```python
        self._check_cancel()
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
        """Sleep `seconds`, but give up at once when a cancel arrives."""
        end = time.monotonic() + seconds
        while True:
            self.check_cancel()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.05, left))
```
and in `send`, right after `self.n += 1`, add `self._wait(self.sc.delay)`.

`tests/test_obd_allowlist.py:35`: change the parametrize filter so delayed scenarios are skipped (their commands are `healthy`'s):

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

### Task 3: ObdJob

**Files:**
- Create: `crank/obd/job.py`
- Test: `tests/test_obd_job.py`

**Interfaces:**
- Consumes: `ObdCancelled`, `ObdError` (Task 1).
- Produces: `ObdJob(run, session_name="", on_finish=None)` where `run(cancel_event) -> readout`. Attributes `state` (`idle|running|done|error|cancelled`), `message`, `readout`, `session_name`, `cancel_event`, `started_at`; methods `start()`, `cancel()`, `join(timeout=None)`; properties `running`, `seconds`. `on_finish()` is called before the final state becomes visible, so a lock released there is free by the time a poller sees `done`.

- [ ] **Step 1: Write the failing tests** (`tests/test_obd_job.py`)

```python
import threading

from crank.obd.errors import ObdCancelled, ObdProtocolError
from crank.obd.job import ObdJob


def job_of(run, **kw):
    j = ObdJob(run, **kw)
    j.start()
    return j


def test_a_finished_read_is_done_with_its_readout():
    j = job_of(lambda ev: "R")
    j.join(5)
    assert j.state == "done" and j.readout == "R" and not j.running and j.message == ""


def test_cancel_ends_a_waiting_read_as_cancelled():
    def run(ev):
        ev.wait(5)
        raise ObdCancelled()
    j = job_of(run)
    assert j.running
    j.cancel(); j.join(5)
    assert j.state == "cancelled" and j.readout is None and "nothing was recorded" in j.message


def test_an_obd_error_is_kept_as_the_message():
    def run(ev):
        raise ObdProtocolError("The vehicle did not answer. Is the key on?")
    j = job_of(run); j.join(5)
    assert j.state == "error" and j.message == "The vehicle did not answer. Is the key on?"


def test_an_unexpected_error_names_its_type():
    def run(ev):
        raise ValueError("boom")
    j = job_of(run); j.join(5)
    assert j.state == "error" and j.message == "ValueError: boom"


def test_a_cancel_after_the_read_finished_does_not_undo_it():
    def run(ev):
        ev.set()                                           # the cancel lands as the read completes
        return "R"
    j = job_of(run); j.join(5)
    assert j.state == "done" and j.readout == "R"


def test_on_finish_runs_before_the_final_state_is_visible():
    seen = []
    holder = {}
    j = ObdJob(lambda ev: "R", on_finish=lambda: seen.append(holder["j"].state))
    holder["j"] = j
    j.start(); j.join(5)
    assert seen == ["running"] and j.state == "done"


def test_seconds_counts_while_running_and_stops_when_finished():
    gate = threading.Event()
    j = job_of(lambda ev: gate.wait(5) and "R")
    assert j.running and j.seconds >= 0
    gate.set(); j.join(5)
    done_at = j.seconds
    assert j.seconds == done_at


def test_a_job_that_was_never_started_is_idle():
    j = ObdJob(lambda ev: "R")
    assert j.state == "idle" and not j.running and j.seconds == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_obd_job.py -q -p no:cacheprovider`
Expected: `ModuleNotFoundError: No module named 'crank.obd.job'`.

- [ ] **Step 3: Implement** (`crank/obd/job.py`)

```python
"""ObdJob: one OBD read in a background thread, so the UI can show it running and cancel it."""
from __future__ import annotations

import threading
import time
from typing import Callable

from .errors import ObdCancelled, ObdError


class ObdJob:
    def __init__(self, run: Callable[[threading.Event], object], session_name: str = "",
                 on_finish: Callable[[], None] | None = None):
        self._run, self.session_name, self._on_finish = run, session_name, on_finish
        self.cancel_event = threading.Event()
        self.state, self.message, self.readout = "idle", "", None
        self.started_at = self.finished_at = 0.0
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.started_at = time.time()
        self.state = "running"
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self.cancel_event.set()

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
            readout = self._run(self.cancel_event)
            state = "done"
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
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add crank/obd/job.py tests/test_obd_job.py
git commit -m "Add ObdJob to run an OBD read in a thread

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Controller start_obd, cancel_obd, obd_job

**Files:**
- Modify: `crank/controller.py` (imports, `__init__`, new methods, `read_obd` becomes a wrapper)
- Test: `tests/test_obd_controller.py`

**Interfaces:**
- Consumes: `ObdJob` (Task 3), `ObdReader(..., cancel=)` (Task 2), `slow_adapter` (Task 2).
- Produces: `Controller.start_obd(source) -> str | None` (message, or `None` once the job runs), `Controller.cancel_obd() -> None`, `Controller.obd_job: ObdJob | None`; `read_obd(source) -> (readout, None) | (None, message)` unchanged.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_obd_controller.py`; add `import time` and `from crank.obd.errors import ObdNoAdapter` and `from crank.obd.transport import SimTransport` if missing)

```python
class Gated(SimTransport):
    """A simulated adapter whose replies wait for `gate` (or a cancel)."""

    def __init__(self, scenario, gate):
        super().__init__(scenario)
        self.gate = gate

    def send(self, cmd, timeout=5.0):
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


def test_the_read_goes_to_the_case_it_was_started_on(tmp_path):
    import threading
    from crank.case import Case
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

            def run(cancel):
                readout = ObdReader(transport, cancel=cancel).read()
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
        job = self.obd_job
        if job and job.running:
            job.cancel()

    def read_obd(self, source="healthy"):
        """One OBD read into the current case, waiting for it to finish.
        Returns (readout, None) or (None, message)."""
        err = self.start_obd(source)
        if err:
            return None, err
        job = self.obd_job
        job.join()
        return (job.readout, None) if job.state == "done" else (None, job.message)
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_obd_controller.py tests/test_controller.py -q -p no:cacheprovider`
Expected: all pass, including the existing `read_obd` tests.

- [ ] **Step 5: Commit**

```bash
git add crank/controller.py tests/test_obd_controller.py
git commit -m "Run OBD reads as a background job the controller can cancel

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Running indicator, Cancel and result on both pages

**Files:**
- Modify: `case_ui.py` (new `obd_panel`; `_obd` uses it)
- Modify: `remote.py` (the Read OBD expander uses it)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `Controller.start_obd`, `cancel_obd`, `obd_job`, `ObdJob.running/state/message/readout/session_name/seconds/join` (Tasks 3 and 4).
- Produces: `case_ui.obd_panel(ctrl, source, key, label, full_width=False)`. Widget keys: the start button uses `key` (`obd_read` on the Case page, `remote_obd` on the phone), Cancel uses `f"{key}_cancel"`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_app.py`; add `import threading` and `import time` at the top, and `from crank.obd.transport import SimTransport`)

```python
def _waiting_read(monkeypatch):
    """Make the next OBD read wait until it is cancelled. Returns the event that lets it go on."""
    gate = threading.Event()

    class Waiting(SimTransport):
        def send(self, cmd, timeout=5.0):
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


def test_phone_remote_shows_a_running_read_and_cancel_stops_it(at, monkeypatch):
    gate = _waiting_read(monkeypatch)
    try:
        at.query_params["page"] = "remote"
        at.run()
        at.button(key="remote_obd").click().run()
        assert any("Reading from the vehicle" in i.value for i in at.info)
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

Run: `python -m pytest tests/test_app.py -q -p no:cacheprovider -k "running_read or another_session"`
Expected: the two running-read tests fail (the button blocks and no info/cancel exist; `at.button(key="obd_read_cancel")` raises `KeyError`/`ElementNotFound`).

- [ ] **Step 3: Implement**

In `case_ui.py` add `import os` and `import time` if not already imported, then add above `_obd`:

```python
def obd_panel(ctrl, source, key: str, label: str, full_width: bool = False) -> None:
    """The start button, the running indicator with Cancel, and the last read's result.

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
        if st.button("Cancel", key=f"{key}_cancel", use_container_width=full_width):
            ctrl.cancel_obd()
            job.join(2.0)
        if job.running:
            refresh = float(os.environ.get("CRANK_REMOTE_REFRESH_S", "1"))
            if refresh > 0:                           # tests set 0 to render once instead of polling
                time.sleep(refresh)
                st.rerun()
            return
    if job.state == "done":
        st.success("Recorded in the case:")
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
(keep `from case_ui import pick_obd_source` where it is; remove the now-unused duplicate import line only if you add it separately).

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_app.py -q -p no:cacheprovider`
Expected: all pass, including the existing `test_case_page_obd_read_records_codes_and_shows_a_result`, `test_case_page_obd_failure_shows_an_error_and_records_nothing` and `test_phone_remote_obd_button`.
If a Streamlit `AppTest` run times out under machine load, re-run that test alone before suspecting the change.

- [ ] **Step 5: Commit**

```bash
git add case_ui.py remote.py
git add -p tests/test_app.py        # take only this task's three tests if the unrelated session fix is still uncommitted
git commit -m "Show a running OBD read with Cancel on the Case page and the phone

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Documentation

**Files:**
- Modify: `README.md` (scenario count and list; a short "Cancelling a read" paragraph)

**Interfaces:**
- Consumes: everything above (documents behaviour only).

- [ ] **Step 1: Check the current wording**

Run: `grep -n "twenty-nine\|Read from OBD\|slow_search" README.md`
Expected: the "twenty-nine scenarios" sentence (about line 105) and the **Read from OBD** paragraph (about line 99).

- [ ] **Step 2: Edit**

Change "twenty-nine scenarios" to "thirty scenarios" and add `slow_adapter` (every reply takes about a second, so you can try Cancel without hardware) to the scenario list in that sentence. After the paragraph that begins "**When the PCM does not give a value.**" add:

```markdown
**Cancelling a read.** A read runs in the background. While it runs, the Case page and the phone's Read OBD show "Reading from the vehicle… N s" and a **Cancel** button, and the Read button is disabled; a second read from another device or tab is refused until it ends. Cancel stops the read within a fraction of a second, even during the protocol search, closes the adapter's port and records nothing: no event and no measurements, and the page says so. A cancelled read cannot be told apart from one never started. The simulator's `slow_adapter` scenario lets you try it without hardware. Not checked against a real adapter yet (#3).
```

- [ ] **Step 3: Run the full suite**

Run: `python -m pytest -q -p no:cacheprovider --ignore=tests/test_app.py` then `python -m pytest tests/test_app.py -q -p no:cacheprovider`
Expected: all pass (the app tests run separately because `AppTest` can time out under heavy machine load).

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "Document cancelling an OBD read

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```
