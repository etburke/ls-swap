# Serial OBD Transport and Read-Only Allowlist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the app read through a real classic-Bluetooth ELM327/STN adapter on a serial port, while making it impossible to send commands that write adapter memory or sleep it.

**Architecture:** A pure `allowlist.check()` gates every command; `SerialTransport` (pyserial, lazily imported, injectable fake) implements the existing `open/send/close` `Transport` interface and calls the allowlist before touching the port. `Controller.read_obd` accepts a source (simulator scenario or serial port) and the two UI pages gain a Simulator / Real adapter choice.

**Tech Stack:** Python 3.11, pyserial>=3.5, pytest, Streamlit.

**Spec:** `docs/superpowers/specs/2026-10-12-serial-obd-transport-design.md`

## Global Constraints

- Allowlist accepts only OBD modes 01, 02, 03, 07, 0A (no 09) and `ATZ ATE0 ATL0 ATS0 ATH0 ATH1 ATSP0 ATDPN ATDP`; everything else (`ATPP`, all `ST*`) is rejected.
- `allowlist.check` runs before anything is written to the port; `SimTransport` does not call it.
- Default baud 115200, one retry at 38400 if `ATZ` gets no prompt.
- `pyserial` imported lazily; `requirements.txt` gains `pyserial>=3.5`.
- Simulator stays the default everywhere; existing tests stay green.
- A dropped port is never swallowed: it raises `ObdNoAdapter`/`ObdTimeout`, never returns partial text.
- Out of scope: multi-ECU, adapter identification, error taxonomy, sleep/wake, `STPX`, BLE. The first real read waits for the multi-ECU slice.

## Review Focus

- Port busy/missing at `open()`: plain-language `ObdNoAdapter` (Task 2).
- Forbidden command never reaches `write`, including with odd case/spacing (Tasks 1, 2).
- Reply split across several reads, `>` arriving in its own chunk (Task 2).
- Unplug after a partial reply: raises, returns no partial text (Task 2).
- Several or zero adapter ports on "auto": error lists candidates (Task 3).

## File Structure

- Create `crank/obd/allowlist.py` — pure command gate.
- Modify `crank/obd/errors.py` — add `ObdForbiddenCommand`.
- Create `crank/obd/serial_transport.py` — `SerialTransport`, `list_ports`, `resolve_port`.
- Modify `crank/controller.py` — `read_obd(source)`.
- Modify `case_ui.py`, `remote.py` — source picker.
- Modify `requirements.txt`, `README.md`.
- Create `tests/test_obd_allowlist.py`, `tests/test_obd_serial.py`; extend `tests/test_obd_controller.py`.

---

### Task 1: Allowlist

**Files:**
- Create: `crank/obd/allowlist.py`
- Modify: `crank/obd/errors.py`
- Test: `tests/test_obd_allowlist.py`

**Interfaces:**
- Produces: `check(cmd: str) -> str` (normalized command or raises `ObdForbiddenCommand`); `ObdForbiddenCommand(ObdError)`.

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from crank.obd.allowlist import check
from crank.obd.errors import ObdError, ObdForbiddenCommand
from crank.obd.reader import ObdReader
from crank.obd.transport import SimTransport, SCENARIOS


@pytest.mark.parametrize("cmd", ["STRSTNVM", "STSAVCAL", "STSLCS", "STSLEEP", "STBTSET", "STWBR 115200",
                                 "ATPP 0C SV 01", "ATPP", "STI", "STPX h:7DF, d:0100", "0902", "04",
                                 "ATZZ", "", "AT", "0", "ATSP6", "ATWS"])
def test_forbidden_commands_are_rejected(cmd):
    with pytest.raises(ObdForbiddenCommand):
        check(cmd)


@pytest.mark.parametrize("cmd,norm", [("atz", "ATZ"), ("AT E0", "ATE0"), (" 010c ", "010C"), ("03", "03"),
                                      ("0a", "0A"), ("07", "07"), ("020200", "020200"), ("ATH1", "ATH1"),
                                      ("ATDPN", "ATDPN"), ("ATSP0", "ATSP0"), ("0100", "0100")])
def test_allowed_commands_are_normalized(cmd, norm):
    assert check(cmd) == norm


def test_forbidden_is_an_obd_error():
    assert issubclass(ObdForbiddenCommand, ObdError)


@pytest.mark.parametrize("name", [n for n, s in SCENARIOS.items() if not s.fault or s.fault.startswith("drop")])
def test_every_command_the_reader_sends_passes(name):
    sent = []

    class Rec(SimTransport):
        def send(self, cmd, timeout=5.0):
            sent.append(cmd)
            return super().send(cmd, timeout)

    try:
        ObdReader(Rec(name)).read()
    except ObdError:
        pass
    assert sent
    for cmd in sent:
        check(cmd)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_obd_allowlist.py -q`
Expected: FAIL (ImportError: allowlist / ObdForbiddenCommand).

- [ ] **Step 3: Implement**

Append to `crank/obd/errors.py`:

```python
class ObdForbiddenCommand(ObdError):
    """A command outside the read-only allowlist; it is never sent to the adapter."""
```

Create `crank/obd/allowlist.py`:

```python
"""Read-only gate for everything sent to a real adapter. An allowlist, not a denylist:
anything not listed (ATPP, every ST* command, mode 04/09, ...) is refused."""
from __future__ import annotations

import re

from .errors import ObdForbiddenCommand

AT_ALLOWED = frozenset({"ATZ", "ATE0", "ATL0", "ATS0", "ATH0", "ATH1", "ATSP0", "ATDPN", "ATDP"})
_OBD = (re.compile(r"01[0-9A-F]{2}"),          # live PID
        re.compile(r"02[0-9A-F]{4}"),          # freeze frame: PID + frame number
        re.compile(r"(?:03|07|0A)"))           # stored / pending / permanent codes


def check(cmd: str) -> str:
    """The normalized command (stripped, uppercase, no spaces), or ObdForbiddenCommand."""
    norm = "".join(cmd.split()).upper()
    if norm in AT_ALLOWED or any(p.fullmatch(norm) for p in _OBD):
        return norm
    raise ObdForbiddenCommand(f"Refused to send {cmd.strip()!r}: only read-only OBD requests are allowed.")
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_obd_allowlist.py -q`
Expected: PASS. If the reader-drift test fails on a real reader command, fix the allowlist (the reader's commands are the ground truth), not the test.

- [ ] **Step 5: Commit**

```bash
git add crank/obd/allowlist.py crank/obd/errors.py tests/test_obd_allowlist.py
git commit -m "Add read-only command allowlist for real OBD adapters

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: SerialTransport

**Files:**
- Create: `crank/obd/serial_transport.py`
- Test: `tests/test_obd_serial.py`

**Interfaces:**
- Consumes: `allowlist.check`, `Transport`, `ObdNoAdapter`, `ObdTimeout`.
- Produces: `SerialTransport(port: str, baud: int = 115200, serial_factory=None)` with `sim = False`. `serial_factory(port, baud, timeout) -> obj` where obj has `write(bytes)`, `read(n) -> bytes` (returns `b""` on timeout), `in_waiting` (int), `reset_input_buffer()`, `close()`. Default factory wraps `serial.Serial(port, baud, timeout=timeout)`. Factory failure (`OSError`/`ValueError`) is translated to `ObdNoAdapter`.

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from crank.obd.errors import ObdForbiddenCommand, ObdNoAdapter, ObdTimeout
from crank.obd.reader import ObdReader
from crank.obd.serial_transport import SerialTransport
from crank.obd.transport import SimTransport


class FakeSerial:
    """Scripted port: each write queues the chunks for the next reads. A chunk that is an
    Exception is raised from read(); when the queue is empty read() returns b'' (a timeout)."""

    def __init__(self, replies=None, on_write=None):
        self.written, self.queue, self.closed = [], [], False
        self.replies, self.on_write = replies or {}, on_write

    in_waiting = 0

    def write(self, data):
        self.written.append(data)
        key = data.decode().strip()
        if self.on_write:
            self.queue.extend(self.on_write(key))
        else:
            self.queue.extend(self.replies.get(key, [b"OK\r\r>"]))

    def read(self, n=1):
        if not self.queue:
            return b""
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def reset_input_buffer(self):
        pass

    def close(self):
        self.closed = True


def make(fake, **kw):
    t = SerialTransport("/dev/cu.fake", serial_factory=lambda port, baud, timeout: fake, **kw)
    return t


def test_send_returns_text_before_prompt_and_writes_cr():
    f = FakeSerial({"010C": [b"41 0C 0B B8\r\r>"]})
    t = make(f); t.open()
    assert t.send("010c", 1.0).strip() == "41 0C 0B B8"
    assert f.written == [b"010C\r"]


def test_reply_split_across_reads_with_prompt_in_its_own_chunk():
    f = FakeSerial({"0100": [b"41 00 ", b"BE 3F", b"\r\r", b">"]})
    t = make(f); t.open()
    assert t.send("0100", 1.0).split() == ["41", "00", "BE", "3F"]


def test_no_prompt_raises_timeout():
    f = FakeSerial({"0100": [b"SEARCHING..."]})
    t = make(f); t.open()
    with pytest.raises(ObdTimeout):
        t.send("0100", 0.2)


def test_unplug_after_partial_reply_raises_and_returns_nothing():
    f = FakeSerial({"0100": [b"41 00 BE", OSError("device disconnected")]})
    t = make(f); t.open()
    with pytest.raises(ObdNoAdapter):
        t.send("0100", 1.0)


def test_forbidden_command_never_reaches_the_port():
    f = FakeSerial()
    t = make(f); t.open()
    for cmd in ("STSAVCAL", "atpp 0c sv 01", "STSLEEP", "04"):
        with pytest.raises(ObdForbiddenCommand):
            t.send(cmd)
    assert f.written == []


def test_send_before_open_raises():
    with pytest.raises(ObdNoAdapter):
        make(FakeSerial()).send("ATZ")


def test_open_failure_is_plain_language():
    def boom(port, baud, timeout):
        raise OSError(16, "Resource busy")
    t = SerialTransport("/dev/cu.fake", serial_factory=boom)
    with pytest.raises(ObdNoAdapter) as e:
        t.open()
    assert "/dev/cu.fake" in str(e.value)


def test_close_twice_is_safe_and_closes_the_port():
    f = FakeSerial()
    t = make(f); t.open(); t.close(); t.close()
    assert f.closed


def test_falls_back_to_38400_when_atz_gets_no_prompt():
    opened = []

    def factory(port, baud, timeout):
        opened.append(baud)
        return FakeSerial({"ATZ": [b"ELM327 v1.5\r\r>"]}) if baud == 38400 else FakeSerial({"ATZ": []})

    t = SerialTransport("/dev/cu.fake", serial_factory=factory, atz_timeout=0.2)
    t.open()
    assert opened == [115200, 38400]


def test_both_bauds_silent_raises_timeout():
    t = SerialTransport("/dev/cu.fake", serial_factory=lambda p, b, to: FakeSerial({"ATZ": []}),
                        atz_timeout=0.2)
    with pytest.raises(ObdTimeout):
        t.open()


def test_full_obd_read_through_serial_transport():
    """Drive SimTransport's replies through the serial framing: ObdReader works end to end."""
    sim = SimTransport("p2119_zero_rpm")
    sim.open()
    f = FakeSerial(on_write=lambda cmd: [(sim.send(cmd) + "\r\r>").encode()])
    t = make(f)
    out = ObdReader(t).read()
    assert out.codes[0]["code"] == "P2119" and not out.sim
    assert f.closed
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_obd_serial.py -q`
Expected: FAIL (ImportError: serial_transport).

- [ ] **Step 3: Implement**

Create `crank/obd/serial_transport.py`:

```python
"""Serial transport for classic-Bluetooth ELM327 / STN adapters (e.g. OBDLink MX+, which appears as a
macOS serial port). Every command goes through the read-only allowlist before it touches the port."""
from __future__ import annotations

import threading
import time

from . import allowlist
from .errors import ObdNoAdapter, ObdTimeout
from .transport import Transport

BAUDS_FALLBACK = 38400


def _default_factory(port: str, baud: int, timeout: float):
    import serial                                     # lazy: only needed with a real adapter
    return serial.Serial(port, baud, timeout=timeout)


class SerialTransport(Transport):
    sim = False

    def __init__(self, port: str, baud: int = 115200, serial_factory=None, atz_timeout: float = 3.0):
        self.port, self.baud, self.atz_timeout = port, baud, atz_timeout
        self._factory = serial_factory or _default_factory
        self._ser = None
        self._lock = threading.Lock()

    def _connect(self, baud: int) -> None:
        try:
            self._ser = self._factory(self.port, baud, 0.1)
        except (OSError, ValueError, ImportError) as e:
            raise ObdNoAdapter(f"Could not open {self.port}: {e}. Is the adapter paired and not in use "
                               "by another app?") from e

    def open(self) -> None:
        bauds = [self.baud] + ([BAUDS_FALLBACK] if self.baud != BAUDS_FALLBACK else [])
        for i, baud in enumerate(bauds):
            self._connect(baud)
            try:
                self.send("ATZ", self.atz_timeout)
                self.baud = baud
                return
            except ObdTimeout:
                self.close()
                if i == len(bauds) - 1:
                    raise ObdTimeout("The adapter did not answer. Check it is plugged in and powered.")

    def send(self, cmd: str, timeout: float = 5.0) -> str:
        norm = allowlist.check(cmd)                   # refuse before anything is written
        if self._ser is None:
            raise ObdNoAdapter("The adapter is not open.")
        with self._lock:
            try:
                self._ser.reset_input_buffer()
                self._ser.write(norm.encode() + b"\r")
                buf, deadline = b"", time.monotonic() + timeout
                while b">" not in buf:
                    if time.monotonic() > deadline:
                        raise ObdTimeout("The adapter did not answer. Check it is plugged in and powered.")
                    buf += self._ser.read(max(1, getattr(self._ser, "in_waiting", 0) or 1))
            except OSError as e:
                raise ObdNoAdapter(f"The adapter connection dropped: {e}.") from e
        return buf.split(b">", 1)[0].decode("ascii", errors="replace")

    def close(self) -> None:
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except OSError:
                pass
```

Note: `FakeSerial.in_waiting` is a class attribute `0`, so `max(1, 0 or 1)` reads one byte at a time. Real pyserial reports the waiting byte count.

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_obd_serial.py tests/test_obd_allowlist.py -q`
Expected: PASS. In `test_full_obd_read...`, `SimTransport.send` handles `ATZ` etc., so the whole read goes through the serial framing.

- [ ] **Step 5: Commit**

```bash
git add crank/obd/serial_transport.py tests/test_obd_serial.py
git commit -m "Add pyserial transport for classic-Bluetooth OBD adapters

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Port discovery

**Files:**
- Modify: `crank/obd/serial_transport.py`
- Test: `tests/test_obd_serial.py`

**Interfaces:**
- Produces: `list_ports(comports=None) -> list[tuple[str, str]]` of `(label, path)`; `resolve_port(port: str, comports=None) -> str`. `comports` is a callable returning objects with `.device` and `.description` (default: `serial.tools.list_ports.comports`).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_obd_serial.py`)

```python
from types import SimpleNamespace

from crank.obd.serial_transport import list_ports, resolve_port


def _ports(*devices):
    return lambda: [SimpleNamespace(device=d, description="x") for d in devices]


def test_list_ports_keeps_only_adapter_looking_ports():
    cp = _ports("/dev/cu.OBDLinkMXPlus", "/dev/cu.Bluetooth-Incoming-Port", "/dev/cu.usbserial-110",
                "/dev/tty.OBDLinkMXPlus", "/dev/cu.MyOBDII")
    assert [p for _, p in list_ports(cp)] == ["/dev/cu.OBDLinkMXPlus", "/dev/cu.usbserial-110",
                                              "/dev/cu.MyOBDII"]


def test_resolve_auto_with_one_match():
    assert resolve_port("auto", _ports("/dev/cu.OBDLinkMXPlus")) == "/dev/cu.OBDLinkMXPlus"


def test_resolve_auto_with_none_or_several_lists_candidates():
    with pytest.raises(ObdNoAdapter) as none:
        resolve_port("auto", _ports("/dev/cu.Bluetooth-Incoming-Port"))
    assert "paired" in str(none.value)
    with pytest.raises(ObdNoAdapter) as many:
        resolve_port("auto", _ports("/dev/cu.OBDLinkA", "/dev/cu.OBDLinkB"))
    assert "/dev/cu.OBDLinkA" in str(many.value) and "/dev/cu.OBDLinkB" in str(many.value)


def test_resolve_explicit_path_is_used_as_is():
    assert resolve_port("/dev/cu.whatever", _ports()) == "/dev/cu.whatever"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_obd_serial.py -q`
Expected: FAIL (ImportError: list_ports).

- [ ] **Step 3: Implement** (append to `crank/obd/serial_transport.py`; add `import fnmatch` at the top)

```python
ADAPTER_GLOBS = ("/dev/cu.OBDLink*", "/dev/cu.*OBD*", "/dev/cu.usbserial*")


def _comports():
    from serial.tools import list_ports as lp          # lazy, like the transport
    return lp.comports()


def list_ports(comports=None) -> list[tuple[str, str]]:
    """(label, path) for serial ports that look like OBD adapters."""
    out = []
    for p in (comports or _comports)():
        if any(fnmatch.fnmatch(p.device, g) for g in ADAPTER_GLOBS):
            out.append((f"{p.device} ({p.description})", p.device))
    return out


def resolve_port(port: str, comports=None) -> str:
    """'auto' picks the only adapter-looking port; anything else is taken as a path."""
    if port != "auto":
        return port
    found = [path for _, path in list_ports(comports)]
    if len(found) == 1:
        return found[0]
    if not found:
        raise ObdNoAdapter("No OBD adapter port found. Pair the adapter in Bluetooth settings, then refresh.")
    raise ObdNoAdapter("More than one possible adapter port: " + ", ".join(found) + ". Choose one.")
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_obd_serial.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add crank/obd/serial_transport.py tests/test_obd_serial.py
git commit -m "Add serial adapter port discovery

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Controller wiring

**Files:**
- Modify: `crank/controller.py:11-13,68-77`
- Test: `tests/test_obd_controller.py`

**Interfaces:**
- Consumes: `SerialTransport`, `resolve_port`.
- Produces: `Controller.read_obd(source="healthy")` where `source` is a scenario name `str`, or `("serial", port_or_"auto")`. Returns `(readout, None)` or `(None, message)`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_obd_controller.py`)

```python
from crank.obd import serial_transport
from crank.obd.errors import ObdNoAdapter
from crank.obd.transport import SimTransport


def _serial_via_sim(monkeypatch, scenario="p2119_zero_rpm"):
    sim = SimTransport(scenario)
    sim.open()

    class Fake:
        written = []
        in_waiting = 0
        queue = []

        def write(self, data):
            self.written.append(data)
            self.queue = list((sim.send(data.decode().strip()) + "\r\r>").encode())

        def read(self, n=1):
            return bytes([self.queue.pop(0)]) if self.queue else b""

        def reset_input_buffer(self): pass
        def close(self): pass

    fake = Fake()
    monkeypatch.setattr(serial_transport, "_default_factory", lambda port, baud, timeout: fake)
    return fake


def test_serial_source_reads_and_records_a_real_readout(tmp_path, monkeypatch):
    fake = _serial_via_sim(monkeypatch)
    c = Controller(SessionStore(tmp_path))
    r, err = c.read_obd(("serial", "/dev/cu.fake"))
    assert err is None and r.codes[0]["code"] == "P2119" and r.sim is False
    assert [e.type for e in c.case().events()].count("code_read") == 1
    assert fake.written and fake.written[0] == b"ATZ\r"


def test_serial_auto_with_no_port_returns_a_message(tmp_path, monkeypatch):
    monkeypatch.setattr("crank.controller.resolve_port",
                        lambda p: (_ for _ in ()).throw(ObdNoAdapter("No OBD adapter port found.")))
    c = Controller(SessionStore(tmp_path))
    r, err = c.read_obd(("serial", "auto"))
    assert r is None and "No OBD adapter" in err and c.case().events() == []


def test_unknown_source_is_a_message_not_a_crash(tmp_path):
    r, err = Controller(SessionStore(tmp_path)).read_obd(("bluetooth", "x"))
    assert r is None and err
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_obd_controller.py -q`
Expected: FAIL (tuple source not handled).

- [ ] **Step 3: Implement.** In `crank/controller.py` change imports and `read_obd`:

```python
from .obd.errors import ObdError
from .obd.reader import ObdReader
from .obd.serial_transport import SerialTransport, resolve_port
from .obd.transport import SimTransport
```

```python
    def _obd_transport(self, source):
        if isinstance(source, str):
            return SimTransport(source)
        kind, value = source
        if kind == "serial":
            return SerialTransport(resolve_port(value))
        raise ObdError(f"Unknown OBD source {kind!r}.")

    def read_obd(self, source="healthy"):
        """One OBD read into the current case. Returns (readout, None) or (None, message).

        `source` is a simulator scenario name, or ("serial", port | "auto") for a real adapter."""
        try:
            readout = ObdReader(self._obd_transport(source)).read()
        except ObdError as e:
            return None, str(e)
        self.case().append_obd(readout)
        return readout, None
```

(`ObdReader.read` already closes the transport on every path.)

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_obd_controller.py tests/test_obd_reader.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add crank/controller.py tests/test_obd_controller.py
git commit -m "Let Controller.read_obd take a serial adapter source

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: UI picker, dependency, docs

**Files:**
- Modify: `case_ui.py:82-95`, `remote.py:38-50`, `requirements.txt`, `README.md`
- Test: `tests/test_app.py` (existing; extend only if it already drives these widgets)

**Interfaces:**
- Consumes: `Controller.read_obd(source)`, `list_ports()`.
- Produces: `pick_obd_source(key: str)` in `case_ui.py` returning a source usable by `read_obd` (scenario str, or `("serial", path)`), or `None` when a real adapter is chosen but no port is available.

- [ ] **Step 1: Check existing UI tests.** Run `grep -n "obd" tests/test_app.py tests/test_cli.py` to see which widget keys (`obd_scenario`, `obd_read`, `remote_obd_scenario`, `remote_obd`) tests rely on; keep those keys and the simulator as the default option so they still pass.

- [ ] **Step 2: Add the picker to `case_ui.py`** (above `_obd`; `import streamlit as st` already exists there):

```python
def pick_obd_source(key: str):
    """Simulator or real adapter. Returns a Controller.read_obd source, or None if no adapter port."""
    mode = st.radio("Source", ["Simulator", "Real adapter"], key=f"{key}_mode", horizontal=True)
    if mode == "Simulator":
        return st.selectbox("Simulated vehicle", list(SCENARIOS), key=f"{key}_scenario")
    from crank.obd.serial_transport import list_ports
    try:
        ports = list_ports()
    except ImportError:
        st.error("Install pyserial (pip install -r requirements.txt) to use a real adapter.")
        return None
    st.button("Refresh ports", key=f"{key}_refresh")
    if not ports:
        st.warning("No adapter port found. Pair the adapter in Bluetooth settings, then refresh.")
        return None
    labels = ["Auto-detect"] + [label for label, _ in ports]
    choice = st.selectbox("Adapter port", labels, key=f"{key}_port")
    if choice == "Auto-detect":
        return ("serial", "auto")
    return ("serial", dict(ports)[choice])
```

Replace `_obd`'s caption and selectbox/button:

```python
    st.caption("A read adds the codes, freeze frame and live values to the case; it never clears codes. "
               "Real-adapter reads are recorded as real, not simulated.")
    source = pick_obd_source("obd")
    if st.button("Read from OBD", key="obd_read", disabled=source is None):
        readout, err = get_controller().read_obd(source)
```

(rest unchanged). The simulator selectbox keeps its old key (`obd_scenario`).

- [ ] **Step 3: Update `remote.py`** the same way: replace the `Read OBD (simulated)` expander body with

```python
    from case_ui import pick_obd_source
    with st.expander("Read OBD"):
        source = pick_obd_source("remote_obd")
        if st.button("Read OBD", key="remote_obd", use_container_width=True, disabled=source is None):
            readout, err = ctrl.read_obd(source)
```

(rest unchanged). Key clash: `pick_obd_source("remote_obd")` makes widget keys `remote_obd_mode`, `remote_obd_scenario`, `remote_obd_port`, `remote_obd_refresh`, and the button is `remote_obd`; no duplicates. If `remote_obd_scenario` was used by a test, it still exists. If importing `case_ui` from `remote.py` pulls in side effects (page config), instead move `pick_obd_source` to a new `obd_ui.py` and import it from both.

- [ ] **Step 4: Dependency and docs.** Append `pyserial>=3.5` to `requirements.txt`. In `README.md`, extend the OBD section: real classic-Bluetooth adapters (OBDLink MX+) read through a serial port, the adapter must be paired in macOS Bluetooth settings first, only read-only requests are ever sent, and **do not do a first real read until multi-ECU handling lands (issue #4, next slice)**.

- [ ] **Step 5: Install and run the full suite.**

Run: `pip install -r requirements.txt && python -m pytest -q`
Expected: all PASS (existing count plus the new tests). If a Streamlit AppTest fails because of a changed widget, fix the picker, not the test.

- [ ] **Step 6: Commit**

```bash
git add case_ui.py remote.py requirements.txt README.md
git commit -m "Add Simulator / Real adapter choice to the OBD read UI

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```
