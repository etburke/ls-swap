# Adapter Timing and Error Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A read survives the ordinary hiccups of a real OBD adapter (slow protocol search, transient bus errors, brown-out reset, a sleeping adapter, two reads at once) and reports the rest in plain language, with every hiccup visible in the case.

**Architecture:** `protocol.py` classifies adapter error text through a table into a new `ObdAdapterError` (`kind`, `transient`, `reset`). `ObdReader` gets a `_request` wrapper that sends a request, parses the reply, and retries once on a transient error or after re-running `INIT` on `LV RESET`, noting each retry in `ObdReadout.notes`; the first `0100` gets a long timeout. `SerialTransport.open` sends `ATZ` twice per baud. `Controller.read_obd` takes a non-blocking lock. `SimTransport` learns to honour timeouts and to inject adapter errors so all of this is tested against fakes.

**Tech Stack:** Python 3, pytest, pyserial (already a dependency, faked in tests).

**Spec:** `docs/superpowers/specs/2026-10-15-adapter-hardening-design.md`

## Global Constraints

- Read-only: no new command may be sent; every command the reader sends must pass `allowlist.check` (the drift test in `tests/test_obd_allowlist.py` runs over every non-faulted scenario, including the new ones).
- Timeouts: default request timeout stays `5.0` s; first `0100` uses `first_timeout=20.0`.
- Retry policy: one retry per request, at most 3 retries per read; only `transient` and `reset` adapter errors are retried; `INIT` commands are not retried.
- A read with no hiccups looks exactly as before: `notes == []`, same lines, same event data.
- Existing `except ObdProtocolError` paths keep working (`ObdAdapterError` subclasses it); messages are plain language for the person at the truck.
- Out of scope (do not build): `STPTO`/per-command timeouts, spinner/job wrapper, auto-reconnect, any `ST*` command.
- Commits end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

- A reply that carries real data lines plus `CAN ERROR` is treated as an error and retried (Task 3).
- A second `LV RESET` after the retry, or a failure while re-running `INIT`, raises instead of looping (Task 3).
- Notes from a retried read reach the stored case event, not just the in-memory readout (Task 3).
- The read lock is released after an `ObdError` and after an unexpected exception (Task 5).
- A first request that times out even at 20 s fails with the existing "adapter did not answer" message, not a hang (Task 3).

---

### Task 1: Adapter error taxonomy

**Files:**
- Modify: `crank/obd/errors.py`
- Modify: `crank/obd/protocol.py:9-30` (the `ERRORS` tuple and `_raise_adapter_errors`)
- Test: `tests/test_obd_protocol.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `ObdAdapterError(message, kind="", transient=False, reset=False)`, a subclass of `ObdProtocolError`, with attributes `.kind` (the adapter's own term, e.g. `"CAN ERROR"`), `.transient`, `.reset`. `protocol.ADAPTER_ERRORS` is the table. `_raise_adapter_errors(lines)` raises `ObdAdapterError` for every table match and keeps the plain `ObdProtocolError` for `?`.

Spec ruling: the spec says `kind` and `transient`; `reset` is added as a third flag so the reader can tell `LV RESET` (re-init, then retry) from a plain transient error without comparing strings. The spec's "longest prefix wins" rule is implemented, but no two table prefixes currently collide (`ERR` is only a bare-`ERR94`-style catch-all); the test pins the rule with `ERR94`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_obd_protocol.py` (and add `ObdAdapterError` to the `crank.obd.errors` import):

```python
@pytest.mark.parametrize("text,kind,transient,reset", [
    ("BUS BUSY", "BUS BUSY", True, False),
    ("BUS ERROR", "BUS ERROR", True, False),
    ("CAN ERROR", "CAN ERROR", True, False),
    ("FB ERROR", "FB ERROR", True, False),
    ("<RX ERROR", "<RX ERROR", True, False),
    ("DATA ERROR", "DATA ERROR", True, False),
    ("FC RX TIMEOUT", "FC RX TIMEOUT", True, False),
    ("LV RESET", "LV RESET", False, True),
    ("BUFFER FULL", "BUFFER FULL", False, False),
    ("OUT OF MEMORY", "OUT OF MEMORY", False, False),
    ("UART RX OVERFLOW", "UART RX OVERFLOW", False, False),
    ("STOPPED", "STOPPED", False, False),
    ("ACT ALERT", "ACT ALERT", False, False),
    ("ERR94", "ERR", False, False),
    ("UNABLE TO CONNECT", "UNABLE TO CONNECT", False, False),
    ("BUS INIT: ERROR", "BUS INIT", False, False),
])
def test_adapter_errors_are_classified(text, kind, transient, reset):
    with pytest.raises(ObdAdapterError) as e:
        P.ecu_messages(text)
    assert (e.value.kind, e.value.transient, e.value.reset) == (kind, transient, reset)


def test_no_vehicle_errors_ask_about_the_key():
    for text in ("UNABLE TO CONNECT", "BUS INIT: ERROR"):
        with pytest.raises(ObdAdapterError, match="Is the key on"):
            P.ecu_messages(text)


def test_lv_reset_points_at_adapter_power():
    with pytest.raises(ObdAdapterError, match="pin 16"):
        P.ecu_messages("LV RESET")


def test_data_lines_do_not_hide_an_adapter_error():
    with pytest.raises(ObdAdapterError) as e:
        P.ecu_messages("7E8 06 41 00 BE 3F A8 13\rCAN ERROR")
    assert e.value.transient


def test_unknown_command_is_still_a_plain_protocol_error():
    with pytest.raises(ObdProtocolError) as e:
        P.ecu_messages("?")
    assert not isinstance(e.value, ObdAdapterError)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_obd_protocol.py -q`
Expected: FAIL — `ImportError: cannot import name 'ObdAdapterError'`.

- [ ] **Step 3: Implement**

In `crank/obd/errors.py`, after `ObdProtocolError`:

```python
class ObdAdapterError(ObdProtocolError):
    """The adapter itself reported a fault. `kind` is the adapter's own term. A `transient` error is
    worth one retry; `reset` means the adapter lost its settings (LV RESET) and must be re-initialised."""

    def __init__(self, message: str, kind: str = "", transient: bool = False, reset: bool = False):
        super().__init__(message)
        self.kind, self.transient, self.reset = kind, transient, reset
```

In `crank/obd/protocol.py` change the import to `from .errors import ObdAdapterError, ObdNotSupported, ObdProtocolError`, then replace the `ERRORS` tuple with:

```python
# (adapter text prefix, class, plain-language message). The longest matching prefix wins.
# Classes: transient (worth one retry), reset (re-run INIT, then retry), fatal, no_vehicle.
ADAPTER_ERRORS = (
    ("UNABLE TO CONNECT", "no_vehicle", "Unable to connect to the vehicle. Is the key on?"),
    ("BUS INIT", "no_vehicle", "Unable to connect to the vehicle (bus initialisation failed). Is the key on?"),
    ("BUS BUSY", "transient", "The CAN bus was busy."),
    ("BUS ERROR", "transient", "The adapter saw a bus error."),
    ("CAN ERROR", "transient", "The adapter had a CAN error (it could not reach the bus)."),
    ("FB ERROR", "transient", "The adapter reported a feedback error on the bus."),
    ("<RX ERROR", "transient", "The adapter received a corrupted frame."),
    ("DATA ERROR", "transient", "The adapter received garbled data."),
    ("FC RX TIMEOUT", "transient", "The vehicle did not send the flow-control frame the adapter waited for."),
    ("LV RESET", "reset", "The adapter reset itself because its supply voltage dipped (LV RESET). "
                          "Check its power on OBD pin 16."),
    ("BUFFER FULL", "fatal", "The adapter's buffer overflowed (BUFFER FULL)."),
    ("OUT OF MEMORY", "fatal", "The adapter ran out of memory."),
    ("UART RX OVERFLOW", "fatal", "The adapter's serial input overflowed."),
    ("STOPPED", "fatal", "The adapter stopped the request before it finished."),
    ("ACT ALERT", "fatal", "The adapter warned it is about to go to sleep (ACT ALERT)."),
    ("ERR", "fatal", "The adapter reported an internal error."),
)
```

and replace `_raise_adapter_errors`:

```python
def _raise_adapter_errors(lines: list[str]) -> None:
    for line in lines:
        if line == "?":
            raise ObdProtocolError("The adapter did not understand the command.")
        hits = [e for e in ADAPTER_ERRORS if line.startswith(e[0])]
        if hits:
            prefix, cls, message = max(hits, key=lambda e: len(e[0]))
            raise ObdAdapterError(message, kind=prefix, transient=cls == "transient", reset=cls == "reset")
```

Then `grep -n "ERRORS" crank tests` and remove or update any remaining use of the old `ERRORS` name.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_obd_protocol.py -q`
Expected: PASS (new tests and the existing `test_adapter_errors_raise`).

- [ ] **Step 5: Commit**

```bash
git add crank/obd/errors.py crank/obd/protocol.py tests/test_obd_protocol.py
git commit -m "Classify adapter errors as transient, reset or fatal

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Simulator honours timeouts and injects adapter errors

**Files:**
- Modify: `crank/obd/transport.py` (`Scenario`, `SCENARIOS`, `SimTransport`)
- Test: `tests/test_obd_transport.py`

**Interfaces:**
- Consumes: nothing from Task 1 (the sim only emits adapter text).
- Produces: `Scenario` fields `search_delay: float = 0.0`, `error_on: str = ""`, `error_text: str = ""`, `error_times: int = 1`. `SimTransport` attributes `searched: bool`, `errors_left: int`. New scenarios `slow_search`, `transient_can_error`, `persistent_can_error`, `lv_reset_mid_read`, `buffer_full`. `SimTransport.send` raises `ObdTimeout` for the first OBD request after `ATZ`/`ATSP0` when `search_delay > timeout`. An `LV RESET` reply also sets `headers = False` and `searched = False` (the adapter forgot its settings).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_obd_transport.py` (add `import pytest` and `from crank.obd.errors import ObdTimeout` to the imports):

```python
def test_search_delay_beyond_the_timeout_times_out_the_first_request_only():
    t = sim("slow_search")
    t.send("ATSP0")
    with pytest.raises(ObdTimeout):
        t.send("0100", 5.0)
    assert t.send("0100", 20.0).startswith("7E8")
    assert t.send("010C", 5.0).startswith("7E8")


def test_injected_error_is_answered_once_then_normal():
    t = sim("transient_can_error")
    assert t.send("010C") == "CAN ERROR"
    assert t.send("010C").startswith("7E8")
    assert t.send("0105").startswith("7E8")


def test_persistent_error_keeps_coming():
    t = sim("persistent_can_error")
    assert [t.send("010C") for _ in range(4)] == ["CAN ERROR"] * 4


def test_lv_reset_makes_the_sim_forget_headers_and_protocol():
    t = sim("lv_reset_mid_read")
    assert t.send("010C") == "LV RESET"
    assert t.headers is False and t.searched is False
    assert not t.send("010C").startswith("7E8")        # headerless until ATH1 is sent again


def test_buffer_full_scenario_answers_the_error_text():
    assert sim("buffer_full").send("010C") == "BUFFER FULL"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_obd_transport.py -q`
Expected: FAIL — `ValueError: unknown OBD scenario 'slow_search'`.

- [ ] **Step 3: Implement**

In `Scenario` add after `interleave`:

```python
    search_delay: float = 0.0                          # seconds the first OBD request takes (protocol search)
    error_on: str = ""                                 # an OBD request that draws an adapter error
    error_text: str = ""                               # e.g. "CAN ERROR"
    error_times: int = 1                               # how many times; 99 = persistent
```

Add to `SCENARIOS`, before `"silent_adapter"`:

```python
    "slow_search": Scenario(values=LIVE_RUNNING, search_delay=8.0),
    "transient_can_error": Scenario(values=LIVE_RUNNING, error_on="010C", error_text="CAN ERROR"),
    "persistent_can_error": Scenario(values=LIVE_RUNNING, error_on="010C", error_text="CAN ERROR",
                                     error_times=99),
    "lv_reset_mid_read": Scenario(values=LIVE_RUNNING, error_on="010C", error_text="LV RESET"),
    "buffer_full": Scenario(values=LIVE_RUNNING, error_on="010C", error_text="BUFFER FULL"),
```

In `SimTransport.__init__` add `self.searched, self.errors_left = False, self.sc.error_times`. In `_at`, set `self.searched = False` for `ATZ` (alongside `self.headers = False`) and for `ATSP0`. In `send`, replace the final `return self._obd(cmd)` with:

```python
        if not self.searched:
            if self.sc.search_delay > timeout:
                raise ObdTimeout("The adapter did not answer. Check it is plugged in and powered.")
            self.searched = True
        if cmd == self.sc.error_on and self.errors_left > 0:
            self.errors_left -= 1
            if self.sc.error_text == "LV RESET":               # the adapter forgot its settings
                self.headers, self.searched = False, False
            return self.sc.error_text
        return self._obd(cmd)
```

(`_at` for `ATSP0` currently falls through to `return "OK"`; add `if cmd == "ATSP0": self.searched = False` before it.)

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_obd_transport.py -q`
Expected: PASS.

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest -q`
Expected: all pass. The new scenarios run through the allowlist drift test (`tests/test_obd_allowlist.py`); with the current reader they raise `ObdProtocolError`-family errors, which that test catches.

- [ ] **Step 6: Commit**

```bash
git add crank/obd/transport.py tests/test_obd_transport.py
git commit -m "Simulate a slow protocol search and injected adapter errors

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Reader — first-request timeout, retry, LV RESET recovery

**Files:**
- Modify: `crank/obd/reader.py` (`ObdReader.__init__`, `_ask`, `_by_ecu`, `read`)
- Test: `tests/test_obd_reader.py`, `tests/test_obd_controller.py`

**Interfaces:**
- Consumes: Task 1's `ObdAdapterError` (`.kind`, `.transient`, `.reset`); Task 2's scenarios and `SimTransport` behaviour.
- Produces: `ObdReader(transport, timeout=5.0, first_timeout=20.0, retry_pause=0.3)`; `ObdReader._request(cmd, parse, timeout=None) -> (resp, parsed)`; constant `MAX_RETRIES = 3`; notes appended to `ObdReadout.notes`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_obd_reader.py` change the imports and helper:

```python
from crank.obd.errors import ObdAdapterError, ObdNoAdapter, ObdProtocolError, ObdTimeout
from crank.obd.pids import LIVE_PIDS
from crank.obd.reader import ObdReader
from crank.obd.transport import LIVE_RUNNING, SCENARIOS, SimTransport


def read(name, **kw):
    return ObdReader(SimTransport(name), retry_pause=0, **kw).read()
```

and append:

```python
def test_slow_protocol_search_succeeds_with_the_long_first_timeout():
    assert live(read("slow_search"))["Engine RPM"].value == 750.0


def test_slow_protocol_search_times_out_if_the_first_timeout_is_short():
    with pytest.raises(ObdTimeout):
        read("slow_search", first_timeout=5.0)


def test_only_the_first_request_gets_the_long_timeout():
    seen = []

    class Rec(SimTransport):
        def send(self, cmd, timeout=5.0):
            seen.append((cmd, timeout))
            return super().send(cmd, timeout)
    ObdReader(Rec("healthy"), retry_pause=0).read()
    assert dict(seen)["0100"] == 20.0
    assert {t for c, t in seen if c != "0100"} == {5.0}


def test_a_clean_read_has_no_notes():
    assert read("healthy").notes == [] and read("p2119_zero_rpm").notes == []


def test_a_transient_error_is_retried_once_and_noted():
    r = read("transient_can_error")
    assert live(r)["Engine RPM"].value == 750.0
    assert r.notes == ["Retried 010C: the adapter reported CAN ERROR."]
    assert "Retried 010C: the adapter reported CAN ERROR." in r.lines()


def test_a_persistent_transient_error_fails_with_the_specific_message():
    with pytest.raises(ObdAdapterError, match="CAN error"):
        read("persistent_can_error")


def test_a_fatal_adapter_error_is_not_retried():
    with pytest.raises(ObdAdapterError, match="buffer overflowed"):
        read("buffer_full")                # error_times=1: a retry would have succeeded


def test_lv_reset_reinitialises_the_adapter_and_the_read_continues():
    sent = []

    class Rec(SimTransport):
        def send(self, cmd, timeout=5.0):
            sent.append(cmd)
            return super().send(cmd, timeout)
    r = ObdReader(Rec("lv_reset_mid_read"), retry_pause=0).read()
    assert live(r)["Engine RPM"].value == 750.0 and r.codes == []
    assert r.notes[0].startswith("The adapter reset (LV RESET) during 010C")
    assert sent.count("ATZ") == 2 and sent.count("ATH1") == 2


def test_lv_reset_retry_uses_the_long_timeout_again():
    seen = []

    class Rec(SimTransport):
        def send(self, cmd, timeout=5.0):
            seen.append((cmd, timeout))
            return super().send(cmd, timeout)
    ObdReader(Rec("lv_reset_mid_read"), retry_pause=0).read()
    assert [t for c, t in seen if c == "010C"] == [5.0, 20.0]


def test_a_second_lv_reset_after_the_retry_is_raised_not_looped():
    t = SimTransport("lv_reset_mid_read")
    t.errors_left = 99                                       # the adapter keeps resetting
    with pytest.raises(ObdAdapterError, match="pin 16"):
        ObdReader(t, retry_pause=0).read()
    assert t.errors_left == 97                               # two attempts, no more


def test_a_failure_while_reinitialising_after_a_reset_is_raised():
    class Dies(SimTransport):
        def send(self, cmd, timeout=5.0):
            if cmd == "ATZ" and self.errors_left == 0:       # the re-init after the reset
                raise ObdTimeout("The adapter stopped answering.")
            return super().send(cmd, timeout)
    with pytest.raises(ObdTimeout):
        ObdReader(Dies("lv_reset_mid_read"), retry_pause=0).read()


def test_a_reply_with_data_and_an_adapter_error_is_retried():
    class Mixed(SimTransport):
        def __init__(self):
            super().__init__("healthy")
            self.first = True

        def _obd(self, cmd):
            out = super()._obd(cmd)
            if cmd == "010C" and self.first:
                self.first = False
                return out + "\rCAN ERROR"
            return out
    r = ObdReader(Mixed(), retry_pause=0).read()
    assert live(r)["Engine RPM"].value == 750.0 and len(r.notes) == 1


def test_retries_are_capped_per_read():
    pids = [f"01{p:02X}" for p in LIVE_PIDS if p in LIVE_RUNNING][:4]

    class Flaky(SimTransport):
        def __init__(self):
            super().__init__("healthy")
            self.seen = set()

        def _obd(self, cmd):
            if cmd in pids and cmd not in self.seen:
                self.seen.add(cmd)
                return "CAN ERROR"
            return super()._obd(cmd)
    with pytest.raises(ObdAdapterError):
        ObdReader(Flaky(), retry_pause=0).read()


def test_a_first_request_that_times_out_even_at_the_long_timeout_fails_clearly():
    with pytest.raises(ObdTimeout, match="did not answer"):
        read("slow_search", first_timeout=1.0)
```

In `tests/test_obd_controller.py` append:

```python
def test_retry_notes_reach_the_stored_case_event(tmp_path):
    c = Controller(SessionStore(tmp_path))
    r, err = c.read_obd("transient_can_error")
    assert err is None
    ev = [e for e in c.case().events() if e.type == "code_read"][0]
    assert ev.data["notes"] == ["Retried 010C: the adapter reported CAN ERROR."]
```

(The controller builds `ObdReader(...)` with the default `retry_pause=0.3`; that single 0.3 s pause is accepted in the controller test.)

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_obd_reader.py tests/test_obd_controller.py -q`
Expected: FAIL — `TypeError: ObdReader.__init__() got an unexpected keyword argument 'retry_pause'`.

- [ ] **Step 3: Implement**

In `crank/obd/reader.py`: import `ObdAdapterError` (`from .errors import ObdAdapterError, ObdNotSupported, ObdProtocolError`), add `MAX_RETRIES = 3` below `STATUS_MODES`, then replace `__init__`, `_ask` and `_by_ecu`:

```python
    def __init__(self, transport: Transport, timeout: float = 5.0, first_timeout: float = 20.0,
                 retry_pause: float = 0.3):
        self.t, self.timeout, self.first_timeout, self.retry_pause = transport, timeout, first_timeout, retry_pause
        self.raw: dict = {}
        self.primary = ""
        self.notes: list = []
        self._retries = 0

    def _ask(self, cmd: str, timeout: float | None = None) -> str:
        resp = self.t.send(cmd, timeout or self.timeout)
        self.raw[cmd] = resp.strip()
        return resp

    def _request(self, cmd: str, parse, timeout: float | None = None):
        """Send an OBD request and parse the reply. A transient adapter error is retried once; LV RESET
        re-runs INIT first. Each retry is noted. Returns (reply text, parse(reply))."""
        retried = False
        while True:
            try:
                resp = self._ask(cmd, timeout)
                return resp, parse(resp)
            except ObdAdapterError as e:
                if retried or self._retries >= MAX_RETRIES or not (e.transient or e.reset):
                    raise
                retried = True
                self._retries += 1
                if e.reset:
                    self.notes.append(f"The adapter reset ({e.kind}) during {cmd}; its settings were "
                                      "re-applied and the request retried.")
                    for init in INIT:
                        self._ask(init)
                    timeout = self.first_timeout                  # the protocol search starts again
                else:
                    self.notes.append(f"Retried {cmd}: the adapter reported {e.kind}.")
                    time.sleep(self.retry_pause)

    def _by_ecu(self, mode: int, pid: int | None = None, frame: bool = False):
        return self._request(P.request(mode, pid, frame),
                             lambda r: P.response_data_by_ecu(r, mode, pid, frame))[1]
```

In `read()`: right after `out = ObdReadout(...)` add `self.notes, self._retries = out.notes, 0`, and replace

```python
            first_raw = self._ask(P.request(0x01, 0x00))      # triggers protocol search
            msgs = P.ecu_messages(first_raw)
```
with
```python
            first_raw, msgs = self._request(P.request(0x01, 0x00), P.ecu_messages,
                                            self.first_timeout)     # triggers protocol search
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_obd_reader.py tests/test_obd_controller.py tests/test_obd_allowlist.py -q`
Expected: PASS, including the drift test over the new scenarios (the re-init commands are all on the allowlist).

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add crank/obd/reader.py tests/test_obd_reader.py tests/test_obd_controller.py
git commit -m "Give the first request a long timeout and retry transient adapter errors

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Wake a sleeping adapter

**Files:**
- Modify: `crank/obd/serial_transport.py` (`open`)
- Test: `tests/test_obd_serial.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `SerialTransport._atz()`; `open()` sends `ATZ` up to twice per baud before falling back to 38400.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_obd_serial.py`:

```python
def test_a_lost_first_atz_is_retried_before_changing_baud():
    opened, seen = [], []

    def on_write(cmd):
        seen.append(cmd)
        return [] if len(seen) == 1 else [b"ELM327 v1.5\r\r>"]

    def factory(port, baud, timeout):
        opened.append(baud)
        return FakeSerial(on_write=on_write)

    SerialTransport("/dev/cu.fake", serial_factory=factory, atz_timeout=0.2).open()
    assert opened == [115200] and seen == ["ATZ", "ATZ"]


def test_both_atz_attempts_silent_at_both_bauds_still_raises():
    seen = []

    def factory(port, baud, timeout):
        return FakeSerial(on_write=lambda cmd: seen.append(cmd) or [])

    with pytest.raises(ObdTimeout):
        SerialTransport("/dev/cu.fake", serial_factory=factory, atz_timeout=0.1).open()
    assert seen == ["ATZ"] * 4
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_obd_serial.py -q`
Expected: FAIL — the first test sees `opened == [115200, 38400]`.

- [ ] **Step 3: Implement**

In `SerialTransport`, add:

```python
    def _atz(self) -> None:
        """ATZ, once more if it gets no prompt: a sleeping MX+ wakes on the connection and can lose the first command."""
        try:
            self.send("ATZ", self.atz_timeout)
        except ObdTimeout:
            self.send("ATZ", self.atz_timeout)
```

and in `open()` replace `self.send("ATZ", self.atz_timeout)` with `self._atz()`.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_obd_serial.py -q`
Expected: PASS (the existing 38400 fallback and both-silent tests still pass; they now take about twice as long).

- [ ] **Step 5: Commit**

```bash
git add crank/obd/serial_transport.py tests/test_obd_serial.py
git commit -m "Send ATZ twice before assuming the wrong baud rate

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Lock around reads on the shared controller

**Files:**
- Modify: `crank/controller.py` (`__init__`, `read_obd`)
- Test: `tests/test_obd_controller.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `Controller._obd_lock`; `read_obd` returns `(None, "An OBD read is already running.")` when a read is in progress.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_obd_controller.py` (add `import threading` and `from crank.obd.transport import SimTransport` to the imports; the file already imports `SimTransport` lower down, so just keep one import):

```python
def test_a_second_read_during_a_running_one_is_refused(tmp_path):
    c = Controller(SessionStore(tmp_path))
    started, release = threading.Event(), threading.Event()

    class Slow(SimTransport):
        def open(self):
            started.set()
            release.wait(5)
            super().open()

    c._obd_transport = lambda source: Slow("healthy")
    out = []
    th = threading.Thread(target=lambda: out.append(c.read_obd("healthy")))
    th.start()
    assert started.wait(5)
    assert c.read_obd("healthy") == (None, "An OBD read is already running.")
    release.set()
    th.join(5)
    assert out[0][1] is None
    assert c.read_obd("healthy")[1] is None                   # released afterwards


def test_the_read_lock_is_released_after_an_obd_error(tmp_path):
    c = Controller(SessionStore(tmp_path))
    assert c.read_obd("silent_adapter")[0] is None
    assert c.read_obd("healthy")[1] is None


def test_the_read_lock_is_released_after_an_unexpected_exception(tmp_path):
    c = Controller(SessionStore(tmp_path))

    def boom(source):
        raise RuntimeError("bug")
    c._obd_transport = boom
    with pytest.raises(RuntimeError):
        c.read_obd("healthy")
    del c._obd_transport
    assert c.read_obd("healthy")[1] is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_obd_controller.py -q`
Expected: FAIL — the second `read_obd` runs a read instead of returning the refusal (`AssertionError`).

- [ ] **Step 3: Implement**

In `Controller.__init__` after `self._lock = threading.Lock()` add `self._obd_lock = threading.Lock()`. Replace `read_obd`'s body:

```python
        if not self._obd_lock.acquire(blocking=False):
            return None, "An OBD read is already running."
        try:
            try:
                readout = ObdReader(self._obd_transport(source)).read()
            except ObdError as e:
                return None, str(e)
            self.case().append_obd(readout)
            return readout, None
        finally:
            self._obd_lock.release()
```

(Keep the existing docstring.)

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_obd_controller.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add crank/controller.py tests/test_obd_controller.py
git commit -m "Refuse a second OBD read while one is running

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Documentation

**Files:**
- Modify: `README.md` (section "Reading the vehicle (OBD-II)", after the multi-ECU paragraph, before "## First session at the truck")

**Interfaces:** none.

- [ ] **Step 1: Add the README text**

Read the section first (`sed -n 97,125p README.md`), then add a short paragraph after the multi-ECU one:

```
The reader gives the first request 20 seconds for the adapter's protocol search (later requests get 5) and retries
a request once when the adapter reports a transient bus error such as `CAN ERROR` or `BUS BUSY`. If the adapter
resets itself (`LV RESET`, usually a supply dip), the reader re-applies its settings and retries once. Every retry
is listed in the read's notes, so a read that needed one says so; other adapter errors (`BUFFER FULL`, `STOPPED`,
and so on) fail the read with a specific message. A second read started while one is running is refused. The
simulator has `slow_search`, `transient_can_error`, `persistent_can_error`, `lv_reset_mid_read` and `buffer_full`
scenarios for these.
```

Also update the scenario count/list sentence in that section if it states a total (13 → 18).

- [ ] **Step 2: Run the whole suite**

Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "Document adapter timing, retries and error handling

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```
