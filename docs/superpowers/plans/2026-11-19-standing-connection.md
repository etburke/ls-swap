# Standing OBD Connection, Status, Relink and Traffic Log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The owner can press Connect to hold the MX+ open, see its state, voltage and whether it is still there, press Relink after a drop, read faster on the open connection, and watch every adapter command and reply in a live log.

**Architecture:** An `ObdConnection` (state machine plus a heartbeat thread that sends `ATRV`) owned by the `Controller`. Reads on an open connection use a `HeldTransport` (open and close do nothing) and a reader that skips the reset and identification. A `LoggingTransport` records every command, reply and error to `TrafficLog`, which writes an app-wide rolling file and a per-session copy. A sidebar status block, a phone status strip and a Log page draw it.

**Tech Stack:** Python 3, `threading`, pyserial (lazy), Streamlit (`st.fragment`, `AppTest` for UI tests), pytest. Run tests with `python -m pytest` (the system `python` has pyserial; `.venv` does not).

**Spec:** `docs/superpowers/specs/2026-11-19-standing-connection-design.md`

## Global Constraints

- Connect and Disconnect are the owner's: no auto-connect, no automatic reconnect. Relink is manual.
- With no connection, a read opens and closes the port itself, exactly as before. `Controller.read_obd` and `start_obd` keep their behaviour and signatures.
- Connect never sends an OBD request (no protocol search). Vehicle status starts as `not checked`.
- The heartbeat is `ATRV` every 5 s (configurable), runs only when it can take the read lock, and two misses in a row (or a connection error) set `dropped` and close the port.
- `ATRV` is the only command added to the allowlist; it is read-only.
- A dropped connection refuses a read with exactly: `The adapter connection dropped. Press Relink, or Disconnect to read without a standing connection.`
- The log is diagnostic only: nothing in `events.jsonl`, the summary or the reports changes. A log write error never breaks a read or the heartbeat.
- Log files: `logs/obd.log` (rotates at 5 000 000 bytes, 3 files in all: `obd.log`, `obd.log.1`, `obd.log.2`) and `sessions/<name>/obd.log` (same limits, no heartbeat lines). Line format: `<ISO time with ms> [<session>] [<tag> ]<kind> <text>` with kinds `>>` command, `<<` reply, `!!` error, `**` event, and tags `hb` (heartbeat) or `sim` (simulated).
- Simulator: 31 scenarios after this slice; the new one is `heartbeat_drop`.
- Existing UI widget keys are not reused: the scope's sidebar Connect button already has key `connect`, so the adapter buttons use `obd_connect`, `obd_disconnect`, `obd_relink` (phone: `remote_obd_connect`, `remote_obd_disconnect`, `remote_obd_relink`).
- Commit only the files a task names. The working tree also holds an uncommitted, unrelated session-creation fix in `app.py` and `tests/test_app.py`: stage files by name, never `git add -A` or `git add .`. `app.py` is also edited by Task 6; when it is still uncommitted then, stage only this task's hunks (build the index copy from `git show HEAD:app.py` plus this task's edit, as was done for `tests/test_app.py` in the previous slice).
- Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

Inputs and conditions the per-task tests do not make obvious; each has a pinning test in the task named:

1. A read started while the heartbeat is sending must wait or be refused, never interleave (Task 5: `test_a_read_waits_for_a_heartbeat_in_flight`).
2. Disconnect while a heartbeat holds the lock must wait for it, then close cleanly (Task 5: `test_disconnect_waits_for_a_heartbeat_in_flight`).
3. Relink with the adapter still off gives a clear error, not a hang (Task 4: `test_relink_with_the_adapter_still_gone_reports_an_error`).
4. A cancelled read on a held connection leaves the port open and the next read does a full setup and works (Task 5: `test_a_cancelled_read_keeps_the_port_open_and_the_next_read_is_cold`).
5. Two Connect presses at once connect once (Task 5: `test_two_connects_connect_once`).
6. A log directory that cannot be written does not break reads or the heartbeat (Task 2: `test_a_write_error_is_counted_and_never_raised`).
7. The log never grows past its limit (Task 2: `test_rotation_keeps_three_files`).
8. The source picker value `"connection"` after the connection closed must not crash (Task 5: `test_a_read_asked_for_the_connection_after_it_closed_is_refused`).
9. The server stopping while connected: the heartbeat is a daemon thread and the OS closes the port. Not testable here; recorded in the final message.

---

### Task 1: ATRV on the allowlist, voltage parsing, simulator heartbeat

**Files:**
- Modify: `crank/obd/allowlist.py` (add `ATRV`)
- Modify: `crank/obd/adapter.py` (add `parse_voltage`)
- Modify: `crank/obd/transport.py` (`atrv_fail_after`, `ATRV` answer, `heartbeat_drop`)
- Test: `tests/test_obd_allowlist.py`, `tests/test_obd_adapter.py`, `tests/test_obd_transport.py`

**Interfaces:**
- Produces: `check("ATRV") == "ATRV"`; `parse_voltage(text: str) -> float | None`; `Scenario.atrv_fail_after: int | None = None`; `SimTransport` answers `ATRV` with `"12.4V"` and raises `ObdTimeout` once more than `atrv_fail_after` answers were given; scenario `heartbeat_drop` (`atrv_fail_after=3`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_obd_adapter.py` (add `parse_voltage` to its `crank.obd.adapter` import):

```python
@pytest.mark.parametrize("text,want", [("12.4V", 12.4), ("\r12.4V\r\r", 12.4), ("ATRV\r11.9V", 11.9),
                                        ("13V", 13.0), ("9.6 V", 9.6)])
def test_parse_voltage_reads_a_voltage(text, want):
    assert parse_voltage(text) == want


@pytest.mark.parametrize("text", ["", "?", "NO DATA", "LV RESET", "ELM327 v1.5", "OK", "12.4"])
def test_parse_voltage_rejects_everything_else(text):
    assert parse_voltage(text) is None
```

Append to `tests/test_obd_allowlist.py`:

```python
def test_atrv_is_allowed_but_not_with_arguments():
    assert check("atrv") == "ATRV" and check(" AT RV ") == "ATRV"
    for bad in ("ATRV1", "ATRV 12", "ATRVX"):
        with pytest.raises(ObdForbiddenCommand):
            check(bad)
```

Append to `tests/test_obd_transport.py`:

```python
def test_the_sim_answers_atrv_with_a_voltage():
    t = SimTransport("healthy"); t.open()
    assert t.send("ATRV").strip() == "12.4V"


def test_heartbeat_drop_stops_answering_atrv_after_three():
    assert "heartbeat_drop" in SCENARIOS
    t = SimTransport("heartbeat_drop"); t.open()
    for _ in range(3):
        assert t.send("ATRV").strip() == "12.4V"
    with pytest.raises(ObdTimeout):
        t.send("ATRV")
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_obd_adapter.py tests/test_obd_allowlist.py tests/test_obd_transport.py -q -p no:cacheprovider`
Expected: collection error `ImportError: cannot import name 'parse_voltage'`.

- [ ] **Step 3: Implement**

`crank/obd/allowlist.py`: add `"ATRV"` to `AT_ALLOWED`.

`crank/obd/adapter.py`: add below `_STN`:

```python
_VOLTS = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*V", re.IGNORECASE)
```
and below `parse_sti`:

```python
def parse_voltage(text: str) -> float | None:
    """The supply voltage in an ATRV reply ('12.4V'), else None. Anything that is not a voltage is a miss."""
    m = _VOLTS.search(text.replace(">", ""))
    return float(m.group(1)) if m else None
```

`crank/obd/transport.py`: add to `Scenario` after `delay`:

```python
    atrv_fail_after: int | None = None                 # ATRV times out after this many answers (heartbeat drop)
```
Add to `SCENARIOS` after `"slow_adapter"`:

```python
    "heartbeat_drop": Scenario(values=LIVE_RUNNING, atrv_fail_after=3),
```
In `SimTransport.__init__` add `self.atrv_n = 0`. In `_at`, before the final `return "OK"`, add:

```python
        if cmd == "ATRV":
            self.atrv_n += 1
            if self.sc.atrv_fail_after is not None and self.atrv_n > self.sc.atrv_fail_after:
                raise ObdTimeout("The adapter did not answer. Check it is plugged in and powered.")
            return "12.4V"
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_obd_adapter.py tests/test_obd_allowlist.py tests/test_obd_transport.py tests/test_obd_reader.py -q -p no:cacheprovider`
Expected: all pass (including the allowlist drift test over every scenario, which now includes `heartbeat_drop`).

- [ ] **Step 5: Commit**

```bash
git add crank/obd/allowlist.py crank/obd/adapter.py crank/obd/transport.py tests/test_obd_adapter.py tests/test_obd_allowlist.py tests/test_obd_transport.py
git commit -m "Allow ATRV, parse its voltage and simulate a heartbeat drop

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Traffic log and the logging transport

**Files:**
- Modify: `crank/obd/transport.py` (add `DelegatingTransport`)
- Create: `crank/obd/trafficlog.py`
- Modify: `.gitignore` (add `logs/`)
- Test: `tests/test_obd_trafficlog.py`

**Interfaces:**
- Produces: `DelegatingTransport(inner)` (a `Transport` whose `sim` and `cancel` read and write through to `inner`; `open`, `send`, `close` delegate). `TrafficLog(logs_dir, session=None, max_bytes=5_000_000, keep=3, now=time.time)` with `path`, `errors`, `last_error`, `event(text, tag="")`, `command(cmd, tag="")`, `reply(text, ms, tag="")`, `error(exc, tag="")`; `session` is a callable returning `(name, Path)` or `None`. `LoggingTransport(inner, log)` (a `DelegatingTransport` that logs). `tail_lines(path, n=200, hide_heartbeats=False) -> list[str]`.

- [ ] **Step 1: Write the failing tests** (`tests/test_obd_trafficlog.py`)

```python
import re

import pytest

from crank.obd.errors import ObdTimeout
from crank.obd.reader import ObdReader
from crank.obd.trafficlog import LoggingTransport, TrafficLog, tail_lines
from crank.obd.transport import SimTransport

LINE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3} \[[^\]]*\] (?:(?:hb|sim) )?(?:>>|<<|!!|\*\*) .*$")


def make(tmp_path, session=None, **kw):
    return TrafficLog(tmp_path / "logs", session=session, **kw)


def lines(path):
    return path.read_text().splitlines()


def test_lines_have_the_documented_format(tmp_path):
    log = make(tmp_path, session=lambda: ("mx", None))
    log.event("connected"); log.command("010C"); log.reply("41 0C\r0B B8\r\r", 43)
    log.error(ObdTimeout("no answer")); log.command("ATRV", tag="hb")
    got = lines(log.path)
    assert all(LINE.match(l) for l in got) and len(got) == 5
    assert "[mx] ** connected" in got[0] and ">> 010C" in got[1]
    assert "<< 41 0C | 0B B8 (43 ms)" in got[2] and "!! ObdTimeout: no answer" in got[3]
    assert "[mx] hb >> ATRV" in got[4]


def test_the_session_copy_has_no_heartbeat_lines(tmp_path):
    sdir = tmp_path / "sessions" / "mx"
    sdir.mkdir(parents=True)
    log = make(tmp_path, session=lambda: ("mx", sdir))
    log.command("010C"); log.command("ATRV", tag="hb"); log.event("hello")
    app, copy = lines(log.path), lines(sdir / "obd.log")
    assert len(app) == 3 and len(copy) == 2 and not any(" hb " in l for l in copy)


def test_no_session_means_only_the_app_log(tmp_path):
    log = make(tmp_path)
    log.event("x")
    assert lines(log.path) and "[-]" in lines(log.path)[0]


def test_rotation_keeps_three_files(tmp_path):
    log = make(tmp_path, max_bytes=200)
    for i in range(60):
        log.event(f"line {i} " + "x" * 40)
    names = sorted(p.name for p in (tmp_path / "logs").iterdir())
    assert names == ["obd.log", "obd.log.1", "obd.log.2"]
    assert all(p.stat().st_size < 400 for p in (tmp_path / "logs").iterdir())


def test_a_write_error_is_counted_and_never_raised(tmp_path):
    blocked = tmp_path / "logs"
    blocked.write_text("a file where the directory should be")
    log = TrafficLog(blocked)
    log.event("x"); log.command("010C")
    assert log.errors == 2 and log.last_error


def test_logging_transport_records_commands_replies_and_errors(tmp_path):
    log = make(tmp_path)
    t = LoggingTransport(SimTransport("healthy"), log)
    t.open(); t.send("ATZ"); t.send("ATI")
    t.close()
    got = lines(log.path)
    assert any("sim >> ATZ" in l for l in got) and any("sim << ELM327 v1.5" in l for l in got)
    silent = LoggingTransport(SimTransport("silent_adapter"), log)
    silent.open()
    with pytest.raises(ObdTimeout):
        silent.send("ATZ")
    assert any("!! ObdTimeout" in l for l in lines(log.path))


def test_logging_transport_passes_sim_and_cancel_through(tmp_path):
    import threading
    inner = SimTransport("healthy")
    t = LoggingTransport(inner, make(tmp_path))
    ev = threading.Event()
    t.cancel = ev
    assert inner.cancel is ev and t.sim is True


def test_a_cancelled_read_leaves_its_commands_in_the_log(tmp_path):
    import threading
    from crank.obd.errors import ObdCancelled
    log, ev = make(tmp_path), threading.Event()
    t = LoggingTransport(SimTransport("healthy"), log)
    inner = t.send

    def send(cmd, timeout=5.0):
        r = inner(cmd, timeout)
        if cmd == "ATI":
            ev.set()
        return r
    t.send = send
    with pytest.raises(ObdCancelled):
        ObdReader(t, cancel=ev, retry_pause=0).read()
    assert any(">> ATI" in l for l in lines(log.path))


def test_tail_lines_of_a_missing_or_empty_file(tmp_path):
    assert tail_lines(tmp_path / "nope.log") == []
    (tmp_path / "e.log").write_text("")
    assert tail_lines(tmp_path / "e.log") == []


def test_tail_lines_returns_the_last_n_in_order(tmp_path):
    p = tmp_path / "t.log"
    p.write_text("".join(f"line {i}\n" for i in range(1000)))
    assert tail_lines(p, 3) == ["line 997", "line 998", "line 999"]
    assert len(tail_lines(p, 5000)) == 1000


def test_tail_lines_of_a_large_file_is_the_end_of_it(tmp_path):
    p = tmp_path / "big.log"
    p.write_text("".join(f"line {i:06d} " + "x" * 80 + "\n" for i in range(50000)))
    got = tail_lines(p, 200)
    assert len(got) == 200 and got[-1].startswith("line 049999")


def test_tail_lines_can_hide_heartbeats(tmp_path):
    log = make(tmp_path)
    for i in range(10):
        log.command("ATRV", tag="hb")
        log.command(f"01{i:02X}")
    shown = tail_lines(log.path, 4, hide_heartbeats=True)
    assert len(shown) == 4 and not any(" hb " in l for l in shown) and ">> 0109" in shown[-1]
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_obd_trafficlog.py -q -p no:cacheprovider`
Expected: `ModuleNotFoundError: No module named 'crank.obd.trafficlog'`.

- [ ] **Step 3: Implement**

`crank/obd/transport.py`: after the `Transport` class add:

```python
class DelegatingTransport(Transport):
    """A transport that passes everything to another one. `sim` and `cancel` read and write through, so
    whoever sets `cancel` on the wrapper reaches the real transport that is waiting."""

    def __init__(self, inner: Transport):
        self.inner = inner

    @property
    def sim(self) -> bool:
        return self.inner.sim

    @property
    def cancel(self):
        return self.inner.cancel

    @cancel.setter
    def cancel(self, value) -> None:
        self.inner.cancel = value

    def open(self) -> None:
        self.inner.open()

    def send(self, cmd: str, timeout: float = 5.0) -> str:
        return self.inner.send(cmd, timeout)

    def close(self) -> None:
        self.inner.close()
```
(also add `"DelegatingTransport"` to `__all__`.)

`crank/obd/trafficlog.py`:

```python
"""The adapter traffic log: every command, reply, error and connection event, with timestamps.
Diagnostic only; it never goes into a case."""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from .errors import ObdError
from .transport import DelegatingTransport

_HB = re.compile(r"^\S+ \[[^\]]*\] hb ")


def _stamp(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + f".{int(t * 1000) % 1000:03d}"


class _File:
    """An append-only file that rotates: `keep` files in all (name, name.1, name.2 for keep=3)."""

    def __init__(self, path: Path, max_bytes: int, keep: int):
        self.path, self.max_bytes, self.keep = Path(path), max_bytes, keep

    def write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
            self._rotate()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(text)

    def _rotate(self) -> None:
        for i in range(self.keep - 1, 0, -1):
            src = self.path if i == 1 else self.path.with_name(f"{self.path.name}.{i - 1}")
            if src.exists():
                src.replace(self.path.with_name(f"{self.path.name}.{i}"))


class TrafficLog:
    def __init__(self, logs_dir, session=None, max_bytes: int = 5_000_000, keep: int = 3, now=time.time):
        self.path = Path(logs_dir) / "obd.log"
        self._app = _File(self.path, max_bytes, keep)
        self._session, self._max, self._keep, self._now = session, max_bytes, keep, now
        self._lock = threading.Lock()
        self.errors, self.last_error = 0, ""

    def _fail(self, e: Exception) -> None:
        self.errors += 1
        self.last_error = f"{type(e).__name__}: {e}"

    def _write(self, kind: str, text: str, tag: str) -> None:
        name, sdir = (self._session() if self._session else None) or ("", None)
        line = f"{_stamp(self._now())} [{name or '-'}] {tag + ' ' if tag else ''}{kind} {text}\n"
        with self._lock:
            try:
                self._app.write(line)
            except OSError as e:                      # a log must never break a read
                self._fail(e)
            if sdir is not None and tag != "hb":
                try:
                    _File(Path(sdir) / "obd.log", self._max, self._keep).write(line)
                except OSError as e:
                    self._fail(e)

    def event(self, text: str, tag: str = "") -> None:
        self._write("**", text, tag)

    def command(self, cmd: str, tag: str = "") -> None:
        self._write(">>", cmd, tag)

    def reply(self, text: str, ms: float, tag: str = "") -> None:
        body = " | ".join(p for p in re.split(r"[\r\n]+", text) if p.strip()) or "(empty)"
        self._write("<<", f"{body} ({ms:.0f} ms)", tag)

    def error(self, exc: Exception, tag: str = "") -> None:
        self._write("!!", f"{type(exc).__name__}: {exc}", tag)


class LoggingTransport(DelegatingTransport):
    """Records every command, reply and error of the transport it wraps."""

    def __init__(self, inner, log: TrafficLog, tag: str = ""):
        super().__init__(inner)
        self.log, self.tag = log, tag

    def _tag(self) -> str:
        return self.tag or ("sim" if self.inner.sim else "")

    def open(self) -> None:
        self.log.event("port open", self._tag())
        self.inner.open()

    def send(self, cmd: str, timeout: float = 5.0) -> str:
        self.log.command(cmd, self._tag())
        start = time.monotonic()
        try:
            reply = self.inner.send(cmd, timeout)
        except ObdError as e:
            self.log.error(e, self._tag())
            raise
        self.log.reply(reply, (time.monotonic() - start) * 1000, self._tag())
        return reply

    def close(self) -> None:
        self.log.event("port close", self._tag())
        self.inner.close()


def tail_lines(path, n: int = 200, hide_heartbeats: bool = False) -> list[str]:
    """The last `n` lines of the file (after dropping heartbeat lines when asked), read from the end."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return []
    block = 64 * 1024
    while True:
        take = min(size, block)
        with open(path, "rb") as f:
            f.seek(size - take)
            data = f.read(take).decode("utf-8", errors="replace")
        got = data.splitlines()
        if take < size:
            got = got[1:]                              # the first line may be cut off
        if hide_heartbeats:
            got = [l for l in got if not _HB.match(l)]
        if len(got) >= n or take >= size:
            return got[-n:] if n else []
        block *= 4
```

`.gitignore`: add a line `logs/`.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_obd_trafficlog.py tests/test_obd_transport.py tests/test_obd_serial.py tests/test_obd_reader.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add crank/obd/transport.py crank/obd/trafficlog.py .gitignore tests/test_obd_trafficlog.py
git commit -m "Add the adapter traffic log and a logging transport

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Held transport and the warm read

**Files:**
- Modify: `crank/obd/errors.py` (add `ObdVehicleSilent`)
- Modify: `crank/obd/transport.py` (add `HeldTransport`)
- Modify: `crank/obd/reader.py` (`adapter=`, `prepare()`, raise `ObdVehicleSilent`)
- Test: `tests/test_obd_reader.py`

**Interfaces:**
- Consumes: `DelegatingTransport` (Task 2).
- Produces: `ObdVehicleSilent(ObdProtocolError)` (raised when the first `0100` gets no answer; message unchanged: `The vehicle did not answer. Is the key on?`); `HeldTransport(inner)` (`open()` and `close()` do nothing); `ObdReader(..., adapter: AdapterInfo | None = None)`; `ObdReader.prepare() -> AdapterInfo` (runs the setup commands and identifies the adapter on an open transport). With `adapter` given, `read()` skips setup and identification and uses it.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_obd_reader.py`; add `from crank.obd.adapter import AdapterInfo`, `from crank.obd.errors import ObdVehicleSilent` and `from crank.obd.reader import INIT` and `from crank.obd.transport import HeldTransport` to the imports)

```python
def test_a_silent_vehicle_raises_the_specific_error_with_the_same_message():
    t = SimTransport("healthy")
    inner = t.send
    t.send = lambda cmd, timeout=5.0: "NO DATA" if cmd == "0100" else inner(cmd, timeout)
    with pytest.raises(ObdVehicleSilent) as e:
        ObdReader(t, retry_pause=0).read()
    assert isinstance(e.value, ObdProtocolError) and "Is the key on?" in str(e.value)


def test_the_reader_takes_its_interrupt_off_the_transport_when_it_is_done():
    ev = threading.Event()
    done = SimTransport("healthy")
    ObdReader(done, cancel=ev, retry_pause=0).read()
    assert done.cancel is None
    cut = sent_by(SimTransport("healthy"))
    inner = cut.send

    def send(cmd, timeout=5.0):
        if cmd == "010C":
            ev.set()
        return inner(cmd, timeout)
    cut.send = send
    with pytest.raises(ObdCancelled):
        ObdReader(cut, cancel=ev, retry_pause=0).read()
    assert cut.cancel is None                              # else the next heartbeat would see a stale cancel


def test_prepare_runs_the_setup_and_identifies_the_adapter():
    t = sent_by(SimTransport("healthy")); t.open()
    info = ObdReader(t, retry_pause=0).prepare()
    assert t.sent[:len(INIT)] == list(INIT) and "STN2255" in info.stn_version and info.supports_st


def test_held_transport_never_opens_or_closes_the_inner_one():
    inner = SimTransport("healthy"); calls = []
    inner.open, inner.close = lambda: calls.append("open"), lambda: calls.append("close")
    h = HeldTransport(inner)
    h.open(); h.close()
    assert calls == [] and h.sim is True


def test_a_warm_read_skips_the_setup_and_the_identification_and_keeps_the_port_open():
    inner = sent_by(SimTransport("healthy")); inner.open()
    closed = []
    inner.close = lambda: closed.append(1)
    info = ObdReader(inner, retry_pause=0).prepare()          # what the connection did when it connected
    inner.sent.clear()
    r = ObdReader(HeldTransport(inner), adapter=info, retry_pause=0).read()
    assert inner.sent[0] == "0100" and "ATZ" not in inner.sent and "ATI" not in inner.sent
    assert r.adapter is info and r.live and not closed


def test_a_warm_read_does_not_repeat_the_adapter_notes():
    inner = SimTransport("flaky_adapter_id"); inner.open()
    info = ObdReader(inner, retry_pause=0).prepare()
    assert info.notes                                         # STSN timed out at connect
    r = ObdReader(HeldTransport(inner), adapter=info, retry_pause=0).read()
    assert not any("identification incomplete" in n for n in r.notes)


def test_a_cold_read_on_a_held_transport_still_does_the_full_setup():
    inner = sent_by(SimTransport("healthy")); inner.open()
    r = ObdReader(HeldTransport(inner), retry_pause=0).read()
    assert inner.sent[:len(INIT)] == list(INIT) and r.adapter.supports_st
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_obd_reader.py -q -p no:cacheprovider`
Expected: collection error `ImportError: cannot import name 'ObdVehicleSilent'`.

- [ ] **Step 3: Implement**

`crank/obd/errors.py`, after `ObdProtocolError`:

```python
class ObdVehicleSilent(ObdProtocolError):
    """The adapter is fine but the vehicle's first reply never came (key off, or the PCM is not answering)."""
```

`crank/obd/transport.py`, after `DelegatingTransport`:

```python
class HeldTransport(DelegatingTransport):
    """A standing connection lent to one read: the reader may 'open' and 'close' it, the port stays up."""

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass
```
(add `"HeldTransport"` to `__all__`).

`crank/obd/reader.py`: import `ObdVehicleSilent` from `.errors`. Add `adapter=None` as the constructor's last parameter and store `self.adapter = adapter` (an `AdapterInfo` or `None`). Add above `read`:

```python
    def prepare(self) -> AdapterInfo:
        """Setup commands and adapter identification on an open transport (what a connection does once)."""
        for cmd in INIT:
            self._ask(cmd)
        return self._identify()
```
In `read()` replace

```python
            for cmd in INIT:
                self._ask(cmd)
            out.adapter = self._identify()
            out.notes.extend(out.adapter.notes)
```
with

```python
            if self.adapter is None:
                out.adapter = self.prepare()
                out.notes.extend(out.adapter.notes)
            else:
                out.adapter = self.adapter                 # a standing connection did this already
```
and change `raise ObdProtocolError("The vehicle did not answer. Is the key on?")` to `raise ObdVehicleSilent("The vehicle did not answer. Is the key on?")`.
Add a helper to `ObdReader`:

```python
    def _release(self) -> None:
        """Take our interrupt off the transport: a held connection outlives this read, and its heartbeat must
        not see a cancel left over from it."""
        if self.cancel is not None or self.stop is not None:
            self.t.cancel = None
```
and call `self._release()` as the first statement of the `except BaseException:` clause (before its `try`) and right before the final `self.t.close()` on the normal path.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_obd_reader.py tests/test_obd_transport.py tests/test_obd_trafficlog.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add crank/obd/errors.py crank/obd/transport.py crank/obd/reader.py tests/test_obd_reader.py
git commit -m "Let a read run on a held connection without repeating the setup

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: ObdConnection

**Files:**
- Create: `crank/obd/connection.py`
- Test: `tests/test_obd_connection.py`

**Interfaces:**
- Consumes: `LoggingTransport`, `TrafficLog` (Task 2), `ObdReader.prepare` (Task 3), `parse_voltage` (Task 1), `ObdError`/`ObdNoAdapter`.
- Produces: `ObdConnection(log=None, heartbeat_s=5.0, misses=2, read_lock=None, now=time.time)`. State `state` in `disconnected|connecting|connected|dropped|error`; `message`; `adapter: AdapterInfo | None`; `voltage`, `voltage_at`, `last_heard`; `vehicle` in `not checked|answered|no answer` and `vehicle_at`; `cold: bool`; `kind` (`real|simulated`); `source`; `pinging: bool`; `transport` (the logging wrapper, or `None`). Methods: `connect(transport, kind="real", source="") -> str | None` (a message on failure), `disconnect()`, `note_vehicle(answered: bool)`, `mark_cold()`, `adopt(readout)`, `status() -> dict`. `read_lock` is the controller's read lock; the heartbeat only runs when it can take it without blocking.

- [ ] **Step 1: Write the failing tests** (`tests/test_obd_connection.py`)

```python
import threading
import time

import pytest

from crank.obd.connection import ObdConnection
from crank.obd.errors import ObdNoAdapter
from crank.obd.trafficlog import TrafficLog
from crank.obd.transport import SimTransport


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def conn():
    c = ObdConnection(heartbeat_s=0.02)
    yield c
    c.disconnect()


def test_connect_brings_the_adapter_up_without_asking_the_vehicle(conn):
    t = SimTransport("healthy")
    sent = []
    inner = t.send
    t.send = lambda cmd, timeout=5.0: (sent.append(cmd), inner(cmd, timeout))[1]
    assert conn.connect(t, kind="simulated", source="healthy") is None
    assert conn.state == "connected" and conn.kind == "simulated" and conn.source == "healthy"
    assert "STN2255" in conn.adapter.label() and conn.voltage == 12.4 and conn.vehicle == "not checked"
    assert not any(c.startswith("01") for c in sent) and "ATRV" in sent and conn.cold is False


def test_status_is_a_plain_dict(conn):
    conn.connect(SimTransport("healthy"), kind="simulated")
    s = conn.status()
    assert s["state"] == "connected" and "STN2255" in s["adapter"] and s["voltage"] == 12.4
    assert s["vehicle"] == "not checked" and s["last_heard_age"] >= 0 and s["kind"] == "simulated"


def test_connect_failure_leaves_an_error_and_the_port_closed(conn):
    t = SimTransport("silent_adapter")
    closed = []
    t.close = lambda: closed.append(1)
    msg = conn.connect(t)
    assert msg and conn.state == "error" and conn.message == msg and closed and conn.transport is None


def test_connect_when_the_port_cannot_open_reports_it(conn):
    t = SimTransport("healthy"); t.sc.fault = "no_adapter"
    assert "No OBD adapter" in conn.connect(t) and conn.state == "error"


def test_a_second_connect_is_refused(conn):
    conn.connect(SimTransport("healthy"))
    assert conn.connect(SimTransport("healthy")) == "Already connected."


def test_disconnect_closes_the_port_and_stops_the_heartbeat(conn):
    t = SimTransport("healthy")
    closed = []
    t.close = lambda: closed.append(1)
    conn.connect(t)
    conn.disconnect()
    assert conn.state == "disconnected" and closed and conn.transport is None
    n = t.atrv_n
    time.sleep(0.15)
    assert t.atrv_n == n


def test_the_heartbeat_updates_the_voltage_and_last_heard(conn):
    conn.connect(SimTransport("healthy"))
    first = conn.last_heard
    assert wait_for(lambda: conn.last_heard > first)


def test_two_missed_heartbeats_drop_the_connection_and_close_the_port():
    c = ObdConnection(heartbeat_s=0.02)
    t = SimTransport("heartbeat_drop")
    closed = []
    inner = t.close
    t.close = lambda: (closed.append(1), inner())
    c.connect(t)
    assert wait_for(lambda: c.state == "dropped") and closed and "heartbeat" in c.message.lower()
    c.disconnect()


def test_a_dropped_connection_can_connect_again():
    c = ObdConnection(heartbeat_s=0.02)
    c.connect(SimTransport("heartbeat_drop"))
    assert wait_for(lambda: c.state == "dropped")
    assert c.connect(SimTransport("healthy")) is None and c.state == "connected"
    c.disconnect()


def test_relink_with_the_adapter_still_gone_reports_an_error():
    c = ObdConnection(heartbeat_s=0.02)
    c.connect(SimTransport("heartbeat_drop"))
    assert wait_for(lambda: c.state == "dropped")
    gone = SimTransport("healthy"); gone.sc.fault = "no_adapter"
    start = time.monotonic()
    assert c.connect(gone) and c.state == "error" and time.monotonic() - start < 2
    c.disconnect()


def test_the_heartbeat_does_not_run_while_a_read_holds_the_lock():
    lock = threading.Lock()
    c = ObdConnection(heartbeat_s=0.02, read_lock=lock)
    t = SimTransport("heartbeat_drop")
    lock.acquire()                                            # a read is running before and while we connect
    c.connect(t)
    time.sleep(0.3)
    assert c.state == "connected" and t.atrv_n <= 1           # no pings, so no misses
    lock.release()
    assert wait_for(lambda: c.state == "dropped")
    c.disconnect()


def test_an_adapter_reset_in_a_heartbeat_marks_the_connection_cold(conn):
    t = SimTransport("healthy")
    conn.connect(t)
    inner = t.send
    t.send = lambda cmd, timeout=5.0: "LV RESET" if cmd == "ATRV" else inner(cmd, timeout)
    assert wait_for(lambda: conn.cold)


def test_heartbeats_are_logged_with_the_hb_tag(tmp_path):
    log = TrafficLog(tmp_path / "logs")
    c = ObdConnection(log=log, heartbeat_s=0.02)
    c.connect(SimTransport("healthy"))
    assert wait_for(lambda: any(" hb >> ATRV" in l for l in log.path.read_text().splitlines()))
    c.disconnect()
    text = log.path.read_text()
    assert "connected" in text and "disconnected" in text


def test_note_vehicle_and_cold_and_adopt(conn):
    conn.connect(SimTransport("healthy"))
    conn.note_vehicle(False)
    assert conn.vehicle == "no answer"
    conn.note_vehicle(True)
    assert conn.vehicle == "answered" and conn.status()["vehicle_age"] >= 0
    conn.mark_cold()
    assert conn.cold
    readout = type("R", (), {"adapter": conn.adapter})()
    conn.adopt(readout)
    assert conn.cold is False and conn.vehicle == "answered"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_obd_connection.py -q -p no:cacheprovider`
Expected: `ModuleNotFoundError: No module named 'crank.obd.connection'`.

- [ ] **Step 3: Implement** (`crank/obd/connection.py`)

```python
"""ObdConnection: the adapter port held open between reads, with a heartbeat so a drop is noticed."""
from __future__ import annotations

import threading
import time

from .adapter import AdapterInfo, parse_voltage
from .errors import ObdError, ObdNoAdapter
from .reader import ObdReader
from .trafficlog import LoggingTransport, TrafficLog

CAN_CONNECT = ("disconnected", "dropped", "error")


class ObdConnection:
    def __init__(self, log: TrafficLog | None = None, heartbeat_s: float = 5.0, misses: int = 2,
                 read_lock: threading.Lock | None = None, now=time.time):
        self.log, self.heartbeat_s, self.misses = log, heartbeat_s, misses
        self.read_lock, self._now = read_lock, now
        self.state, self.message = "disconnected", ""
        self.adapter: AdapterInfo | None = None
        self.voltage = self.voltage_at = self.last_heard = None
        self.vehicle, self.vehicle_at = "not checked", None
        self.cold, self.kind, self.source = False, "", ""
        self.pinging = False
        self._inner = None
        self.transport = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._misses = 0

    # -- log helpers
    def _event(self, text: str, tag: str = "") -> None:
        if self.log:
            self.log.event(text, tag)

    def _tag(self) -> str:
        return "sim" if self.kind == "simulated" else ""

    # -- control
    def connect(self, transport, kind: str = "real", source="") -> str | None:
        """Open the port, run the setup commands, identify the adapter, read its voltage and start the
        heartbeat. Never sends an OBD request. Returns a message on failure (state 'error')."""
        if self.state not in CAN_CONNECT:
            return "Already connected." if self.state == "connected" else "Already connecting."
        self.state, self.message, self.kind, self.source = "connecting", "", kind, source
        self._event("connecting", self._tag())
        wrapped = LoggingTransport(transport, self.log) if self.log else transport
        try:
            wrapped.open()
            reader = ObdReader(wrapped)
            adapter = reader.prepare()
            self.voltage = parse_voltage(wrapped.send("ATRV", 2.0))
        except ObdError as e:
            self._close(wrapped)
            self.state, self.message, self.transport, self._inner = "error", str(e), None, None
            self._event(f"connect failed: {e}", self._tag())
            return self.message
        self._inner, self.transport, self.adapter = transport, wrapped, adapter
        self.voltage_at = self.last_heard = self._now()
        self.vehicle, self.vehicle_at, self.cold, self._misses = "not checked", None, False, 0
        self.state = "connected"
        self._event(f"connected: {adapter.label()}", self._tag())
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, args=(self._stop,), daemon=True)
        self._thread.start()
        return None

    def disconnect(self) -> None:
        was = self.state
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(3.0)
        self._thread = None
        self._close(self.transport)
        self.transport = self._inner = None
        if was != "disconnected":
            self.state, self.message = "disconnected", ""
            self._event("disconnected", self._tag())

    @staticmethod
    def _close(t) -> None:
        if t is not None:
            try:
                t.close()
            except Exception:                         # closing must never raise
                pass

    # -- heartbeat
    def _beat(self, stop: threading.Event) -> None:
        while not stop.wait(self.heartbeat_s):
            if self.state != "connected":
                continue
            if self.read_lock is not None and not self.read_lock.acquire(blocking=False):
                continue
            self.pinging = True
            try:
                self._ping()
            finally:
                self.pinging = False
                if self.read_lock is not None:
                    self.read_lock.release()

    def _ping(self) -> None:
        tag = "hb"
        if self.log:
            self.log.command("ATRV", tag)
        start = time.monotonic()
        try:
            reply = self._inner.send("ATRV", 2.0)
        except ObdNoAdapter as e:
            self._log_error(e)
            self._drop(f"heartbeat failed: {e}")
            return
        except ObdError as e:
            self._log_error(e)
            self._miss(f"heartbeat got no answer ({e})")
            return
        if self.log:
            self.log.reply(reply, (time.monotonic() - start) * 1000, tag)
        v = parse_voltage(reply)
        if v is None:
            if "RESET" in reply.upper():
                self.cold = True                      # the adapter forgot its settings
            self._miss(f"heartbeat got {reply.strip() or 'nothing'!r} instead of a voltage")
            return
        self.voltage, self.voltage_at, self.last_heard, self._misses = v, self._now(), self._now(), 0

    def _log_error(self, e: Exception) -> None:
        if self.log:
            self.log.error(e, "hb")

    def _miss(self, why: str) -> None:
        self._misses += 1
        self._event(f"heartbeat miss {self._misses}: {why}", "hb")
        if self._misses >= self.misses:
            self._drop(why)

    def _drop(self, why: str) -> None:
        self._stop.set()
        self._close(self.transport)
        self.transport = self._inner = None
        self.state, self.message = "dropped", why
        self._event(f"dropped: {why}", self._tag())

    # -- facts the controller feeds in
    def note_vehicle(self, answered: bool) -> None:
        self.vehicle, self.vehicle_at = ("answered" if answered else "no answer"), self._now()

    def mark_cold(self) -> None:
        self.cold = True

    def adopt(self, readout) -> None:
        """A read on this connection finished: keep the adapter it identified and the vehicle's answer."""
        self.adapter = readout.adapter or self.adapter
        self.cold = False
        self.note_vehicle(True)

    def status(self) -> dict:
        now = self._now()

        def age(t):
            return None if t is None else max(0.0, now - t)
        return {"state": self.state, "message": self.message,
                "adapter": self.adapter.label() if self.adapter and self.state == "connected" else "",
                "voltage": self.voltage, "voltage_age": age(self.voltage_at),
                "last_heard_age": age(self.last_heard), "vehicle": self.vehicle,
                "vehicle_age": age(self.vehicle_at), "kind": self.kind, "source": self.source,
                "cold": self.cold}
```

Note: `connect` after a heartbeat drop must replace the thread: `_drop` set `_stop`, so the old thread exits; `connect` creates a new `_stop` and thread.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_obd_connection.py tests/test_obd_trafficlog.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add crank/obd/connection.py tests/test_obd_connection.py
git commit -m "Add ObdConnection with a heartbeat that notices a drop

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Controller integration

**Files:**
- Modify: `crank/controller.py` (constructor, `get_controller`, `_take_obd_lock`, `start_obd`, new `connect` / `disconnect` / `relink` / `connection_status`)
- Test: `tests/test_obd_controller.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `Controller(store, poll_s=0.2, heartbeat_s=5.0, logs_dir=None)` with `traffic: TrafficLog` (logs dir default: `store.root.parent / "logs"`; `get_controller` reads `CRANK_LOGS_DIR` and `CRANK_HEARTBEAT_S`) and `connection: ObdConnection`; `connect(source) -> str | None`, `disconnect() -> None`, `relink() -> str | None`, `connection_status() -> dict` (the connection's status plus `reading: bool`, `traffic_error: str`). `start_obd` uses the open connection (source `"connection"` or any value is ignored while connected), refuses a dropped one, and wraps the per-read path in `LoggingTransport`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_obd_controller.py`; reuse `Gated`, `Controller`, `SessionStore`, `SimTransport`, `threading`, `time`; add `from crank.obd.errors import ObdNoAdapter` if missing)

```python
DROPPED = ("The adapter connection dropped. Press Relink, or Disconnect to read without a standing connection.")


def conn_controller(tmp_path, **kw):
    c = Controller(SessionStore(tmp_path / "sessions"), heartbeat_s=kw.pop("heartbeat_s", 0.02), **kw)
    return c


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_connect_status_and_disconnect(tmp_path):
    c = conn_controller(tmp_path)
    assert c.connection_status()["state"] == "disconnected"
    assert c.connect("healthy") is None
    s = c.connection_status()
    assert s["state"] == "connected" and s["kind"] == "simulated" and s["reading"] is False
    c.disconnect()
    assert c.connection_status()["state"] == "disconnected"


def test_connect_refuses_the_other_kind_the_case_already_has(tmp_path):
    c = conn_controller(tmp_path)
    c.read_obd("healthy")                                  # a simulated read is in the case

    class RealLike(SimTransport):
        sim = False
    c._obd_transport = lambda source: RealLike("healthy")
    msg = c.connect("x")
    assert msg and "already has simulated" in msg and c.connection_status()["state"] == "disconnected"


def test_connect_returns_a_transport_failure(tmp_path, monkeypatch):
    c = conn_controller(tmp_path)

    def no_port(value):
        raise ObdNoAdapter("No OBD adapter found.")
    monkeypatch.setattr("crank.controller.resolve_port", no_port)
    assert c.connect(("serial", "auto")) == "No OBD adapter found."


def test_two_connects_connect_once(tmp_path):
    c = conn_controller(tmp_path)
    results = []
    ts = [threading.Thread(target=lambda: results.append(c.connect("healthy"))) for _ in range(2)]
    [t.start() for t in ts]; [t.join(5) for t in ts]
    assert sorted(r is None for r in results) == [False, True]
    c.disconnect()


def test_a_read_on_the_open_connection_is_warm_and_keeps_the_port_open(tmp_path):
    c = conn_controller(tmp_path, heartbeat_s=60)
    c.connect("p2119_zero_rpm")
    sent = []
    inner = c.connection._inner.send
    c.connection._inner.send = lambda cmd, timeout=5.0: (sent.append(cmd), inner(cmd, timeout))[1]
    r, err = c.read_obd("ignored")
    assert err is None and r.codes[0]["code"] == "P2119" and "ATZ" not in sent and "ATI" not in sent
    assert c.connection_status()["state"] == "connected" and c.connection.vehicle == "answered"
    assert [e.type for e in c.case().events()].count("code_read") == 1
    c.disconnect()


def test_a_cancelled_read_keeps_the_port_open_and_the_next_read_is_cold(tmp_path):
    c = conn_controller(tmp_path, heartbeat_s=60)
    c.connect("healthy")
    inner = c.connection._inner
    gate = threading.Event()
    real_send = inner.send

    def slow(cmd, timeout=5.0):
        if cmd == "0100":
            while not gate.is_set():
                inner.check_cancel()
                time.sleep(0.01)
        return real_send(cmd, timeout)
    inner.send = slow
    assert c.start_obd("x") is None
    c.cancel_obd(); c.obd_job.join(5)
    assert c.obd_job.state == "cancelled" and c.case().events() == []
    assert c.connection_status()["state"] == "connected" and c.connection.cold
    gate.set()
    inner.send = real_send
    sent = []
    inner.send = lambda cmd, timeout=5.0: (sent.append(cmd), real_send(cmd, timeout))[1]
    r, err = c.read_obd("x")
    assert err is None and "ATZ" in sent and c.connection.cold is False      # full setup, then warm again
    c.disconnect()


def test_a_silent_vehicle_is_noted_on_the_connection(tmp_path):
    c = conn_controller(tmp_path, heartbeat_s=60)
    c.connect("no_vehicle")
    r, err = c.read_obd("x")
    assert r is None and err and c.connection.vehicle == "no answer" and c.connection.cold
    c.disconnect()


def test_a_dropped_connection_refuses_a_read_with_the_relink_message(tmp_path):
    c = conn_controller(tmp_path)
    c.connect("heartbeat_drop")
    assert wait_for(lambda: c.connection_status()["state"] == "dropped")
    assert c.start_obd("healthy") == DROPPED
    c.disconnect()
    assert c.start_obd("healthy") is None                  # without a connection it reads on its own port
    c.obd_job.join(10)


def test_relink_reconnects_with_the_same_source(tmp_path):
    c = conn_controller(tmp_path)
    c.connect("heartbeat_drop")
    assert wait_for(lambda: c.connection_status()["state"] == "dropped")
    assert c.relink() is None and c.connection_status()["state"] == "connected"
    c.disconnect()


def test_relink_with_nothing_to_relink_says_so(tmp_path):
    assert "Press Connect" in conn_controller(tmp_path).relink()


def test_a_read_asked_for_the_connection_after_it_closed_is_refused(tmp_path):
    c = conn_controller(tmp_path)
    msg = c.start_obd("connection")
    assert msg and "Press Connect" in msg


def test_connect_and_disconnect_are_refused_while_a_read_runs(tmp_path):
    c = conn_controller(tmp_path)
    assert c.start_obd("slow_adapter") is None
    assert "read is running" in c.connect("healthy") and c.connection_status()["reading"] is True
    c.cancel_obd(); c.obd_job.join(5)


def test_a_read_waits_for_a_heartbeat_in_flight(tmp_path):
    c = conn_controller(tmp_path, heartbeat_s=0.01)
    c.connect("healthy")
    release = threading.Event()
    inner = c.connection._inner.send

    def slow_atrv(cmd, timeout=5.0):
        if cmd == "ATRV":
            release.wait(2)
        return inner(cmd, timeout)
    c.connection._inner.send = slow_atrv
    assert wait_for(lambda: c.connection.pinging)
    threading.Timer(0.2, release.set).start()
    assert c.start_obd("x") is None                        # waited for the ping instead of refusing
    c.obd_job.join(10)
    assert c.obd_job.state == "done"
    c.disconnect()


def test_disconnect_waits_for_a_heartbeat_in_flight(tmp_path):
    c = conn_controller(tmp_path, heartbeat_s=0.01)
    c.connect("healthy")
    release = threading.Event()
    inner = c.connection._inner.send
    c.connection._inner.send = lambda cmd, timeout=5.0: (release.wait(2), inner(cmd, timeout))[1] if cmd == "ATRV" \
        else inner(cmd, timeout)
    assert wait_for(lambda: c.connection.pinging)
    threading.Timer(0.2, release.set).start()
    c.disconnect()
    assert c.connection_status()["state"] == "disconnected"


def test_every_read_leaves_a_trace_in_the_log_even_when_cancelled(tmp_path):
    c = conn_controller(tmp_path)
    assert c.start_obd("slow_adapter") is None
    time.sleep(0.3)
    c.cancel_obd(); c.obd_job.join(5)
    text = c.traffic.path.read_text()
    assert "read started" in text and "read cancelled" in text and ">> ATZ" in text


def test_a_log_that_cannot_be_written_does_not_break_a_read(tmp_path):
    blocked = tmp_path / "logs"
    blocked.write_text("not a directory")
    c = Controller(SessionStore(tmp_path / "sessions"), logs_dir=blocked)
    r, err = c.read_obd("healthy")
    assert err is None and r.live
    assert c.connection_status()["traffic_error"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_obd_controller.py -q -p no:cacheprovider`
Expected: failures: `TypeError: Controller.__init__() got an unexpected keyword argument 'heartbeat_s'`.

- [ ] **Step 3: Implement** (`crank/controller.py`)

Imports: add `from pathlib import Path`; `from .obd.connection import ObdConnection`; `from .obd.errors import ObdAdapterError, ObdCancelled, ObdError, ObdVehicleSilent`; `from .obd.trafficlog import LoggingTransport, TrafficLog`; `from .obd.transport import HeldTransport, SimTransport`.

Constructor: change the signature to `def __init__(self, store: SessionStore, poll_s: float = 0.2, heartbeat_s: float = 5.0, logs_dir=None):` and, after `self._obd_lock = threading.Lock()`, add:

```python
        self.traffic = TrafficLog(logs_dir or Path(store.root).parent / "logs", session=self._log_session)
        self.connection = ObdConnection(log=self.traffic, heartbeat_s=heartbeat_s, read_lock=self._obd_lock)
```
Add the helpers:

```python
    def _log_session(self):
        return self.session_name, self.store.open(self.session_name).path

    def _take_obd_lock(self) -> bool:
        """The read lock, waiting briefly if only the heartbeat has it (a ping lasts a moment)."""
        if self._obd_lock.acquire(blocking=False):
            return True
        if self.connection.pinging:
            return self._obd_lock.acquire(timeout=3.0)
        return False
```
Replace `start_obd` with:

```python
    DROPPED = ("The adapter connection dropped. Press Relink, or Disconnect to read without a standing "
               "connection.")

    def start_obd(self, source="healthy") -> str | None:
        """Start one OBD read into the current case in the background. Returns a message if it could not
        start, else None. `source` is a simulator scenario name, or ("serial", port | "auto") for a real adapter;
        it is ignored while a standing connection is up.

        The lock is held until the job ends, so a second read from the phone or another tab is refused."""
        if not self._take_obd_lock():
            return "An OBD read is already running."
        started = False
        try:
            conn = self.connection
            held = conn if conn.state == "connected" else None
            if conn.state == "dropped":
                return self.DROPPED
            if held:
                transport = HeldTransport(conn.transport)
            else:
                if source == "connection":
                    return "The connection is closed. Press Connect, or pick an adapter."
                try:
                    transport = LoggingTransport(self._obd_transport(source), self.traffic)
                except ObdError as e:
                    return str(e)
            wrong = self._wrong_kind(transport)
            if wrong:
                return wrong
            case = self.case()                              # the read goes to the case it was started on
            adapter = held.adapter if held and not held.cold else None

            def run(cancel, stop):
                self.traffic.event(f"read started ({'open connection' if held else 'own port'}, "
                                   f"{'warm' if adapter else 'full setup'})")
                try:
                    readout = ObdReader(transport, cancel=cancel, stop=stop, adapter=adapter).read()
                except ObdCancelled:
                    self.traffic.event("read cancelled")
                    if held:
                        held.mark_cold()
                    raise
                except ObdError as e:
                    self.traffic.event(f"read failed: {e}")
                    if held:
                        held.mark_cold()
                        if isinstance(e, ObdVehicleSilent) or (
                                isinstance(e, ObdAdapterError) and e.kind in ("UNABLE TO CONNECT", "BUS INIT")):
                            held.note_vehicle(False)
                    raise
                if held:
                    held.adopt(readout)
                case.append_obd(readout)                    # last step: a cancelled or failed read adds nothing
                self.traffic.event("read stopped" if readout.stopped else "read finished")
                return readout
            job = ObdJob(run, session_name=self.session_name, on_finish=self._obd_lock.release)
            self.obd_job = job
            job.start()
            started = True
            return None
        finally:
            if not started:
                self._obd_lock.release()
```
Add the connection methods (after `read_obd`):

```python
    def connect(self, source) -> str | None:
        """Hold the adapter open. Returns a message if it could not."""
        if not self._take_obd_lock():
            return "A read is running; wait for it or cancel it first."
        try:
            if self.connection.state == "connected":
                return "Already connected."
            try:
                transport = self._obd_transport(source)
            except ObdError as e:
                return str(e)
            wrong = self._wrong_kind(transport)
            if wrong:
                return wrong
            return self.connection.connect(transport, kind="simulated" if transport.sim else "real",
                                           source=source)
        finally:
            self._obd_lock.release()

    def disconnect(self) -> None:
        if not self._take_obd_lock():
            return
        try:
            self.connection.disconnect()
        finally:
            self._obd_lock.release()

    def relink(self) -> str | None:
        """Close the connection and open it again with the same source."""
        source = self.connection.source
        if not source:
            return "Nothing to relink. Press Connect."
        self.disconnect()
        if self.connection.state != "disconnected":
            return "A read is running; wait for it or cancel it first."
        return self.connect(source)

    def connection_status(self) -> dict:
        s = self.connection.status()
        s["reading"] = bool(self.obd_job and self.obd_job.running)
        s["traffic_error"] = self.traffic.last_error
        return s
```
`get_controller`:

```python
            _controller = Controller(SessionStore(os.environ.get("CRANK_SESSIONS_DIR", "sessions")),
                                     float(os.environ.get("CRANK_POLL_S", "0.2")),
                                     float(os.environ.get("CRANK_HEARTBEAT_S", "5")),
                                     os.environ.get("CRANK_LOGS_DIR") or None)
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_obd_controller.py tests/test_controller.py tests/test_obd_connection.py -q -p no:cacheprovider`
Expected: all pass, including every earlier controller test.

- [ ] **Step 5: Commit**

```bash
git add crank/controller.py tests/test_obd_controller.py
git commit -m "Let the controller hold, drop and relink the adapter connection and log every read

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Status block, phone strip, Log page

**Files:**
- Create: `conn_ui.py` (status text helpers, `connection_panel`, `render_log`)
- Modify: `app.py` (sidebar block, `PAGES`, page dispatch)
- Modify: `remote.py` (status strip at the top)
- Modify: `case_ui.py` (`_obd` shows "Using the open connection")
- Test: `tests/test_conn_ui.py`, `tests/test_app.py`

**Interfaces:**
- Consumes: `Controller.connect/disconnect/relink/connection_status`, `Controller.traffic`, `case_ui.pick_obd_source`, `tail_lines`.
- Produces: `conn_ui.status_text(s: dict) -> str`, `conn_ui.vehicle_line(s: dict) -> str`, `conn_ui.connection_panel(ctrl, key: str)` (widget keys `{key}_connect`, `{key}_disconnect`, `{key}_relink`, adapter picker keys from `pick_obd_source(f"{key}_src")`), `conn_ui.render_log(ctrl)` (toggles keyed `log_pause`, `log_hide_hb`). Sidebar key `obd`; phone key `remote_obd`. A page named `Log`.

- [ ] **Step 1: Write the failing tests**

`tests/test_conn_ui.py` (pure helpers):

```python
from conn_ui import status_text, vehicle_line


def s(**kw):
    base = {"state": "disconnected", "message": "", "adapter": "", "voltage": None, "voltage_age": None,
            "last_heard_age": None, "vehicle": "not checked", "vehicle_age": None, "kind": "", "source": "",
            "cold": False, "reading": False, "traffic_error": ""}
    base.update(kind=kw.pop("kind", ""), **kw)
    return base


def test_status_text_for_each_state():
    assert status_text(s()) == "⚪ Disconnected"
    assert status_text(s(state="connecting")) == "🟠 Connecting…"
    assert status_text(s(state="connected", adapter="OBDLink MX+ (STN2255 v5.12.4)", voltage=12.4,
                         last_heard_age=2.2)) == "🟢 Connected, OBDLink MX+ (STN2255 v5.12.4), 12.4 V, last heard 2 s ago"
    assert status_text(s(state="dropped", message="heartbeat failed")) == "🔴 Dropped: heartbeat failed. Press Relink."
    assert status_text(s(state="error", message="No OBD adapter found.")) == "🔴 No OBD adapter found."


def test_status_text_without_a_voltage():
    assert "V," not in status_text(s(state="connected", adapter="A", last_heard_age=1.0))


def test_status_text_marks_a_simulated_connection():
    assert "(simulated)" in status_text(s(state="connected", adapter="A", last_heard_age=1.0, kind="simulated"))


def test_vehicle_line():
    assert vehicle_line(s()) == "Vehicle: not checked"
    assert vehicle_line(s(vehicle="answered", vehicle_age=30.0)) == "Vehicle: answered 30 s ago"
    assert vehicle_line(s(vehicle="no answer", vehicle_age=5.0)) == "Vehicle: no answer (is the key on?)"
```

Append to `tests/test_app.py` (keep the imports it already has; the fixture sets the sessions dir, so also set the logs dir and a short heartbeat in a local fixture):

```python
@pytest.fixture
def at_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CRANK_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("CRANK_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("CRANK_HEARTBEAT_S", "0.05")
    monkeypatch.setenv("CRANK_POLL_S", "0")
    monkeypatch.setenv("CRANK_REMOTE_REFRESH_S", "0")
    reset_controller()
    app = AppTest.from_file(APP, default_timeout=60)
    app.run()
    assert not app.exception
    yield app
    get_controller().disconnect()
    reset_controller()


def test_sidebar_shows_the_connection_and_its_buttons(at_conn):
    assert any("Disconnected" in m.value for m in at_conn.sidebar.markdown)
    assert at_conn.sidebar.button(key="obd_connect").disabled is False
    assert at_conn.sidebar.button(key="obd_disconnect").disabled is True


def test_connect_from_the_sidebar_with_the_simulator_then_disconnect(at_conn):
    at_conn.sidebar.radio(key="obd_src_mode").set_value("Simulator").run()
    at_conn.sidebar.button(key="obd_connect").click().run()
    assert not at_conn.exception
    assert get_controller().connection_status()["state"] == "connected"
    assert any("Connected" in m.value for m in at_conn.sidebar.markdown)
    assert at_conn.sidebar.button(key="obd_connect").disabled is True
    at_conn.sidebar.button(key="obd_disconnect").click().run()
    assert get_controller().connection_status()["state"] == "disconnected"


def test_a_dropped_connection_shows_dropped_and_relink_brings_it_back(at_conn):
    ctrl = get_controller()
    assert ctrl.connect("heartbeat_drop") is None
    deadline = time.time() + 5
    while time.time() < deadline and ctrl.connection_status()["state"] != "dropped":
        time.sleep(0.05)
    at_conn.run()
    assert any("Dropped" in m.value for m in at_conn.sidebar.markdown)
    at_conn.sidebar.button(key="obd_relink").click().run()
    assert ctrl.connection_status()["state"] == "connected"


def test_the_case_page_says_it_uses_the_open_connection(at_conn):
    get_controller().connect("healthy")
    at_conn.run()
    assert any("Using the open connection" in c.value for c in at_conn.caption)
    at_conn.button(key="obd_read").click().run()
    assert any("Recorded in the case" in s.value for s in at_conn.success)


def test_the_phone_page_shows_the_connection_status(at_conn):
    at_conn.query_params["page"] = "remote"
    at_conn.run()
    assert any("Disconnected" in m.value for m in at_conn.markdown)
    assert at_conn.button(key="remote_obd_connect")


def test_the_log_page_tails_the_log_and_can_hide_heartbeats(at_conn):
    ctrl = get_controller()
    ctrl.traffic.event("hello from the test")
    ctrl.traffic.command("ATRV", tag="hb")
    at_conn.sidebar.radio(key="page").set_value("Log").run()
    assert not at_conn.exception
    code = " ".join(c.value for c in at_conn.code)
    assert "hello from the test" in code and "hb >> ATRV" not in code     # heartbeats hidden by default
    at_conn.toggle(key="log_hide_hb").set_value(False).run()
    assert "hb >> ATRV" in " ".join(c.value for c in at_conn.code)
    at_conn.toggle(key="log_pause").set_value(True).run()
    assert not at_conn.exception
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_conn_ui.py tests/test_app.py -q -p no:cacheprovider -k "conn_ui or sidebar_shows or connect_from or dropped_connection or open_connection or phone_page_shows or log_page"`
Expected: `ModuleNotFoundError: No module named 'conn_ui'` and the app tests fail.

- [ ] **Step 3: Implement**

`conn_ui.py`:

```python
"""Connection status, the Connect / Disconnect / Relink buttons and the live log page."""
import streamlit as st

from crank.obd.trafficlog import tail_lines

ICON = {"connected": "🟢", "connecting": "🟠", "dropped": "🔴", "error": "🔴", "disconnected": "⚪"}


def status_text(s: dict) -> str:
    state = s["state"]
    if state == "connecting":
        return "🟠 Connecting…"
    if state == "connected":
        volts = f", {s['voltage']:.1f} V" if s.get("voltage") is not None else ""
        heard = s.get("last_heard_age")
        sim = " (simulated)" if s.get("kind") == "simulated" else ""
        return (f"🟢 Connected{sim}, {s['adapter']}{volts}"
                + (f", last heard {heard:.0f} s ago" if heard is not None else ""))
    if state == "dropped":
        return f"🔴 Dropped: {s['message']}. Press Relink."
    if state == "error":
        return f"🔴 {s['message']}"
    return "⚪ Disconnected"


def vehicle_line(s: dict) -> str:
    if s["vehicle"] == "answered" and s.get("vehicle_age") is not None:
        return f"Vehicle: answered {s['vehicle_age']:.0f} s ago"
    if s["vehicle"] == "no answer":
        return "Vehicle: no answer (is the key on?)"
    return "Vehicle: not checked"


def connection_panel(ctrl, key: str) -> None:
    """Status, vehicle line, adapter picker and the three buttons. Refreshes itself every 2 s."""
    from case_ui import pick_obd_source

    @st.fragment(run_every=2)
    def panel():
        s = ctrl.connection_status()
        st.markdown(status_text(s))
        st.caption(vehicle_line(s))
        if s.get("traffic_error"):
            st.warning(f"The log could not be written: {s['traffic_error']}")
        busy = s["reading"]
        up = s["state"] == "connected"
        source = None if up else pick_obd_source(f"{key}_src")
        c1, c2, c3 = st.columns(3)
        if c1.button("Connect", key=f"{key}_connect", disabled=up or busy or source is None,
                     use_container_width=True):
            err = ctrl.connect(source)
            if err:
                st.error(err)
            st.rerun()
        if c2.button("Disconnect", key=f"{key}_disconnect", disabled=(not up and s["state"] != "dropped") or busy,
                     use_container_width=True):
            ctrl.disconnect()
            st.rerun()
        if c3.button("Relink", key=f"{key}_relink", disabled=busy or not s["source"], use_container_width=True):
            err = ctrl.relink()
            if err:
                st.error(err)
            st.rerun()
    panel()


def render_log(ctrl) -> None:
    st.header("Log")
    st.caption(f"Every adapter command and reply: {ctrl.traffic.path}")
    c1, c2 = st.columns(2)
    paused = c1.toggle("Pause", key="log_pause")
    hide = c2.toggle("Hide heartbeats", value=True, key="log_hide_hb")

    @st.fragment(run_every=None if paused else 1)
    def tail():
        got = tail_lines(ctrl.traffic.path, 200, hide_heartbeats=hide)
        st.code("\n".join(got) if got else "(no log lines yet)", language=None)
    tail()
```

`app.py`: add `import conn_ui` next to `import case_ui`; change `PAGES` to `["Case", "Capture", "Results", "Guided tests", "Compare", "Log"]`; inside the sidebar, after the `st.radio("Page", PAGES, key="page")` line add:

```python
    st.divider()
    st.subheader("OBD adapter")
    conn_ui.connection_panel(ctrl, "obd")
```
and add `"Log": lambda: conn_ui.render_log(ctrl)` to the final page dispatch dict.

`remote.py`: in `render`, after `st.caption(f"{snap['mode']}: …")` add:

```python
    from conn_ui import connection_panel
    with st.expander("OBD adapter connection"):
        connection_panel(ctrl, "remote_obd")
```
(the status text shows inside the expander; the phone test only needs the markdown and the Connect button to exist, and `AppTest` finds elements inside expanders).

`case_ui.py`: in `_obd()` replace `source = pick_obd_source("obd")` with:

```python
    ctrl = get_controller()
    if ctrl.connection_status()["state"] == "connected":
        st.caption("Using the open connection.")
        source = "connection"
    else:
        source = pick_obd_source("obd")
```
and use `ctrl` in the `obd_panel(...)` call. In `remote.py`, make the same change where the Read OBD expander builds `source` (`ctrl.connection_status()["state"] == "connected"` → caption and `source = "connection"`, else `pick_obd_source("remote_obd")`).

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_conn_ui.py tests/test_app.py -q -p no:cacheprovider`
Expected: all pass, including every earlier app test. If an `AppTest` run times out under machine load, re-run that test alone before suspecting the change.

- [ ] **Step 5: Commit**

```bash
git add conn_ui.py remote.py case_ui.py tests/test_conn_ui.py
git add -p app.py tests/test_app.py   # take only this task's hunks: the unrelated session-creation fix stays out
git commit -m "Show the adapter connection, Relink and a live log tail

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Documentation

**Files:**
- Modify: `README.md` (scenario count and list; a "Holding the adapter open" paragraph; the Log page; layout list)

**Interfaces:**
- Consumes: everything above (documents behaviour only).

- [ ] **Step 1: Check the current wording**

Run: `grep -n "thirty scenarios\|Stopping or cancelling a read\|crank/obd/ \|crank_capture.py    command" README.md | cut -c1-90`
Expected: the "thirty scenarios" sentence, the **Stopping or cancelling a read.** paragraph and the project-layout block.

- [ ] **Step 2: Edit**

Change "thirty scenarios" to "thirty-one scenarios" and add `heartbeat_drop` (the adapter stops answering its heartbeat after three beats, so you can watch Dropped and Relink) to the scenario list in that sentence. After the "**Stopping or cancelling a read.**" paragraph add:

```markdown
**Holding the adapter open.** Press **Connect** in the sidebar's "OBD adapter" block (or in the phone page's "OBD adapter connection" section) to keep the adapter open between reads; **Disconnect** closes it, so the MX+ is on the air only when you choose. The block shows the state (Disconnected, Connecting, Connected, Dropped, or the error), the adapter, its battery voltage, how long ago it last answered, and what the last read learned about the vehicle ("not checked" until a read finds out; Connect never asks the vehicle anything). While connected, the app sends the adapter a read-only `ATRV` (battery voltage) every five seconds when no read is running; two missed answers in a row mark the connection **Dropped** and close the port, and **Relink** opens it again (it is manual, nothing reconnects on its own). Reads on the open connection skip the reset and identification, so only the first one does the protocol search. Press Read with no connection and it opens and closes the port itself, as before. A cancelled or failed read leaves the port open and the next read does a full setup. A dropped connection refuses a read until you press Relink or Disconnect. The simulator's `heartbeat_drop` scenario lets you try this without hardware. Not checked against a real adapter yet (#3).

**The log.** Every command and reply the adapter sees, every error and every connection or read event is written to `logs/obd.log` (about 5 MB per file, three files kept) and, without the heartbeat lines, to `sessions/<name>/obd.log` for the active session, including reads that were cancelled or failed. Each line has a time, the session, and `>>` (sent), `<<` (received), `!!` (error) or `**` (event); heartbeat lines are tagged `hb` and simulated traffic `sim`. The **Log** page shows the last 200 lines live, with a Pause toggle and a switch to hide heartbeats. The log is diagnostic: it is never part of the case or the report, and a log that cannot be written never stops a read (the sidebar says so).
```
Add `conn_ui.py` (connection status, buttons, the Log page) to the project-layout block next to `case_ui.py`, and `crank/obd/connection.py` and `crank/obd/trafficlog.py` to the `crank/obd/` line.

- [ ] **Step 3: Run the full suite**

Run: `python -m pytest -q -p no:cacheprovider --ignore=tests/test_app.py` then `python -m pytest tests/test_app.py -q -p no:cacheprovider`
Expected: all pass (the app tests run separately because `AppTest` can time out under heavy machine load).

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "Document the standing connection, Relink and the log

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```
