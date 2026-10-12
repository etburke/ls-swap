# OBD-II Reader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read trouble codes, freeze frame and a live-data snapshot from an ELM327-style OBD-II adapter (simulator only for now) and record them in the current case.

**Architecture:** A pure protocol layer (`crank/obd/protocol.py`, `pids.py`) parses ELM327 text; an `ObdReader` runs one read over a swappable `Transport` (`SimTransport` now; serial and BLE later); `Case.append_obd` turns a readout into `code_read` and `measurement` events, so the existing summary and renderers show it. The Case page and the phone remote get a "Read from OBD" button.

**Tech Stack:** Python 3, pytest, Streamlit (`AppTest`). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-11-obd-reader-design.md`

**Base:** branch `case-file` (PR #2). Run tests with `.venv/bin/python -m pytest -q`; 167 pass before this plan starts.

## Global Constraints

- Never invent a value: a PID the PCM does not answer is "not supported" (`value is None`), never 0. A real 0 stays 0.
- Simulated reads are labeled `[SIMULATED]` everywhere they are shown, like simulator captures.
- No clear-codes command anywhere in v1.
- A failed read appends nothing to the case and shows the error message.
- Single user, local; nothing is uploaded. No new dependencies.
- Existing tests stay green.

## Review Focus

- A clean OBD read after an earlier read with codes must not leave the old codes showing as active (Task 3 `test_a_later_clean_read_marks_earlier_codes_as_not_in_latest_read`).
- A manual code entry with no codes must still be rejected; only OBD reads may be empty (Task 3).
- 0 RPM from the PCM is kept as 0 and the conditions text must not say the engine is off (Task 3).
- NO DATA vs "not supported" must stay distinct: NO DATA is "nothing to report", only a `7F` negative response marks a mode unsupported (Tasks 1 and 2).
- Adapter text that is not hex (garbage, odd length) raises a readable error instead of being parsed (Task 1).
- User/ECU text in reports is HTML-escaped (Task 3).

---

### Task 1: Protocol layer (errors, PIDs, parsing)

**Files:**
- Create: `crank/obd/__init__.py` (empty), `crank/obd/errors.py`, `crank/obd/pids.py`, `crank/obd/protocol.py`
- Test: `tests/test_obd_protocol.py`

**Interfaces:**
- Produces: errors `ObdError`, `ObdNoAdapter`, `ObdTimeout`, `ObdProtocolError`, `ObdNotSupported(ObdProtocolError)`; `PIDS: dict[int, Pid]`, `LIVE_PIDS`, `FREEZE_PIDS`, `DTC_DESCRIPTIONS`; `protocol.request(mode, pid=None, frame=False) -> str`, `messages(resp) -> list[bytes] | None`, `response_data(resp, mode, pid=None, frame=False) -> bytes | None`, `decode_dtc(b0, b1) -> str`, `decode_dtcs(data, can) -> list[str]`, `describe_dtc(code) -> str | None`, `supported_pids(base, data) -> set[int]`, `decode_pid(pid, data) -> (value, unit) | None`, `is_can(protocol_text) -> bool`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_obd_protocol.py`:

```python
import pytest

from crank.obd import protocol as P
from crank.obd.errors import ObdProtocolError


def test_request_strings():
    assert P.request(1, 0x0C) == "010C"
    assert P.request(3) == "03"
    assert P.request(2, 0x0C, frame=True) == "020C00"


def test_single_frame_is_stripped_of_noise_and_decoded():
    resp = "SEARCHING...\r41 0C 1A F8 \r\r>"
    assert P.response_data(resp, 1, 0x0C) == bytes([0x1A, 0xF8])
    assert P.decode_pid(0x0C, bytes([0x1A, 0xF8])) == (1726.0, "rpm")


def test_multi_frame_can_response_is_reassembled_and_truncated():
    resp = "009\r0: 43 04 01 33 02 36\r1: 00 16 00 00 00 00 00\r>"
    assert P.response_data(resp, 3) == bytes([0x04, 0x01, 0x33, 0x02, 0x36, 0x00, 0x16, 0x00])


def test_no_data_is_none_not_empty():
    assert P.response_data("NO DATA\r>", 1, 0x0C) is None


@pytest.mark.parametrize("text", ["UNABLE TO CONNECT", "BUS INIT: ERROR", "CAN ERROR", "?", "STOPPED"])
def test_adapter_errors_raise(text):
    with pytest.raises(ObdProtocolError):
        P.messages(text)


def test_garbage_raises_with_a_readable_message():
    with pytest.raises(ObdProtocolError, match="Unreadable"):
        P.messages("@#$%^")


def test_odd_length_hex_is_unreadable_not_silently_trimmed():
    with pytest.raises(ObdProtocolError):
        P.messages("410C1AF")


def test_negative_response_means_mode_not_supported():
    from crank.obd.errors import ObdNotSupported
    with pytest.raises(ObdNotSupported):
        P.response_data("7F 0A 11", 0x0A)


def test_reply_to_a_different_request_is_an_error():
    with pytest.raises(ObdProtocolError, match="Unexpected reply"):
        P.response_data("41 05 7B", 1, 0x0C)


@pytest.mark.parametrize("b0,b1,code", [(0x03, 0x35, "P0335"), (0x41, 0x00, "C0100"), (0x81, 0x23, "B0123"),
                                         (0xC1, 0x00, "U0100"), (0x21, 0x19, "P2119")])
def test_dtc_letters_digits_and_hex(b0, b1, code):
    assert P.decode_dtc(b0, b1) == code


def test_can_dtc_list_skips_count_byte_and_padding():
    assert P.decode_dtcs(bytes([2, 0x03, 0x35, 0x21, 0x19, 0, 0]), can=True) == ["P0335", "P2119"]


def test_non_can_dtc_list_has_no_count_byte():
    assert P.decode_dtcs(bytes([0x03, 0x35, 0, 0, 0, 0]), can=False) == ["P0335"]


def test_empty_dtc_reply_is_no_codes():
    assert P.decode_dtcs(b"", can=True) == []


def test_supported_pid_bitmap():
    sup = P.supported_pids(0x00, bytes([0xBE, 0x1F, 0xA8, 0x13]))
    assert {0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0C, 0x0D, 0x0F, 0x11, 0x13, 0x15, 0x1C, 0x1F, 0x20} <= sup
    assert 0x02 not in sup and 0x08 not in sup


@pytest.mark.parametrize("pid,data,value", [
    (0x04, bytes([0xFF]), 100.0), (0x05, bytes([0x82]), 90), (0x06, bytes([0x80]), 0.0),
    (0x0C, bytes([0, 0]), 0.0), (0x0D, bytes([60]), 60), (0x11, bytes([0]), 0.0),
    (0x42, bytes([0x36, 0xB0]), 14.0), (0x0F, bytes([0x4B]), 35)])
def test_pid_formulas(pid, data, value):
    assert P.decode_pid(pid, data)[0] == pytest.approx(value)


def test_short_pid_data_is_undecodable_not_zero():
    assert P.decode_pid(0x0C, bytes([0x1A])) is None


@pytest.mark.parametrize("text,can", [("A6", True), ("6", True), ("A9", True), ("A2", False), ("1", False)])
def test_protocol_number_means_can_or_not(text, can):
    assert P.is_can(text) is can


def test_known_and_unknown_descriptions():
    assert "Throttle" in P.describe_dtc("P2119") and P.describe_dtc("P9999") is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_obd_protocol.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'crank.obd'`.

- [ ] **Step 3: Implement**

Run `mkdir -p crank/obd && touch crank/obd/__init__.py`, then:

Create `crank/obd/errors.py`:

```python
"""Errors raised by the OBD layer. The message is written for the person at the truck."""


class ObdError(Exception):
    pass


class ObdNoAdapter(ObdError):
    pass


class ObdTimeout(ObdError):
    pass


class ObdProtocolError(ObdError):
    pass


class ObdNotSupported(ObdProtocolError):
    """The PCM answered with a negative response: it does not support this service."""
```

Create `crank/obd/pids.py`:

```python
"""Mode 01 / 02 PIDs as data: id, name, unit, decoder over the data bytes after the PID."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Pid:
    id: int
    name: str
    unit: str
    nbytes: int
    decode: Callable[[bytes], float]


def _u16(b: bytes) -> int:
    return b[0] * 256 + b[1]


PIDS = {p.id: p for p in (
    Pid(0x04, "Engine load", "%", 1, lambda b: b[0] * 100 / 255),
    Pid(0x05, "Coolant temperature", "°C", 1, lambda b: b[0] - 40),
    Pid(0x06, "Short term fuel trim bank 1", "%", 1, lambda b: b[0] * 100 / 128 - 100),
    Pid(0x07, "Long term fuel trim bank 1", "%", 1, lambda b: b[0] * 100 / 128 - 100),
    Pid(0x0B, "Intake manifold pressure", "kPa", 1, lambda b: b[0]),
    Pid(0x0C, "Engine RPM", "rpm", 2, lambda b: _u16(b) / 4),
    Pid(0x0D, "Vehicle speed", "km/h", 1, lambda b: b[0]),
    Pid(0x0F, "Intake air temperature", "°C", 1, lambda b: b[0] - 40),
    Pid(0x11, "Throttle position", "%", 1, lambda b: b[0] * 100 / 255),
    Pid(0x42, "Control module voltage", "V", 2, lambda b: _u16(b) / 1000),
)}

LIVE_PIDS = (0x0C, 0x42, 0x05, 0x0D, 0x04, 0x06, 0x07, 0x11, 0x0F, 0x0B)
FREEZE_PIDS = (0x0C, 0x05, 0x04, 0x06, 0x07, 0x0B, 0x0D, 0x0F, 0x11)

DTC_DESCRIPTIONS = {
    "P0016": "Crankshaft/camshaft position correlation, bank 1 sensor A",
    "P0335": "Crankshaft position sensor A circuit",
    "P0336": "Crankshaft position sensor A range/performance",
    "P0340": "Camshaft position sensor A circuit, bank 1",
    "P0341": "Camshaft position sensor A range/performance, bank 1",
    "P2119": "Throttle actuator control throttle body range/performance",
    "P0300": "Random/multiple cylinder misfire detected",
    "P0171": "System too lean, bank 1",
    "P0174": "System too lean, bank 2",
    "U0100": "Lost communication with ECM/PCM",
}
```

Create `crank/obd/protocol.py`:

```python
"""Pure ELM327 / OBD-II parsing and request building. No I/O."""
from __future__ import annotations

import re

from .errors import ObdNotSupported, ObdProtocolError
from .pids import DTC_DESCRIPTIONS, PIDS

ERRORS = ("UNABLE TO CONNECT", "BUS INIT", "CAN ERROR", "BUS BUSY", "BUFFER FULL", "STOPPED",
          "FB ERROR", "DATA ERROR", "ERR", "ACT ALERT", "LV RESET")
NOISE = ("SEARCHING", "ELM327", "OK")
_HEX = re.compile(r"^[0-9A-F]+$")


def _lines(resp: str) -> list[str]:
    out = []
    for raw in re.split(r"[\r\n]+", resp.replace(">", "")):
        line = raw.strip().upper()
        if line and not any(line.startswith(n) for n in NOISE):
            out.append(line)
    return out


def messages(resp: str) -> list[bytes] | None:
    """Reassemble the ECU messages in a response. None means 'NO DATA'.

    Raises ObdProtocolError for adapter errors and for text that is not hex.
    """
    lines = _lines(resp)
    if not lines or lines == ["NO DATA"]:
        return None
    for line in lines:
        if line == "?":
            raise ObdProtocolError("The adapter did not understand the command.")
        if any(line.startswith(e) for e in ERRORS):
            raise ObdProtocolError(f"The adapter reported: {line.title()}.")
    msgs, i = [], 0
    while i < len(lines):
        line = lines[i]
        if re.fullmatch(r"[0-9A-F]{3}", line) and i + 1 < len(lines) and lines[i + 1].startswith("0:"):
            want, body, i = int(line, 16), [], i + 1
            while i < len(lines) and re.match(r"^\d+:", lines[i]):
                body.append(lines[i].split(":", 1)[1].replace(" ", ""))
                i += 1
            msgs.append(_hex("".join(body))[:want])
            continue
        msgs.append(_hex(line.replace(" ", "")))
        i += 1
    return msgs


def _hex(s: str) -> bytes:
    if not _HEX.match(s) or len(s) % 2:
        raise ObdProtocolError(f"Unreadable response from the adapter: {s[:24]!r}.")
    return bytes.fromhex(s)


def request(mode: int, pid: int | None = None, frame: bool = False) -> str:
    """e.g. request(1, 0x0C) -> '010C'; request(2, 0x0C, frame=True) -> '020C00'."""
    s = f"{mode:02X}" + (f"{pid:02X}" if pid is not None else "")
    return s + ("00" if frame else "")


def response_data(resp: str, mode: int, pid: int | None = None, frame: bool = False) -> bytes | None:
    """The data bytes of the first message answering this mode/PID; None for NO DATA."""
    msgs = messages(resp)
    if msgs is None:
        return None
    head = bytes([mode + 0x40] + ([pid] if pid is not None else []) + ([0] if frame else []))
    for m in msgs:
        if m.startswith(head):
            return m[len(head):]
        if len(m) >= 3 and m[0] == 0x7F and m[1] == mode:
            raise ObdNotSupported(f"The PCM does not support mode {mode:02X} (negative response {m[2]:02X}).")
    raise ObdProtocolError(f"Unexpected reply to mode {mode:02X}: {msgs[0].hex().upper()}.")


def decode_dtc(b0: int, b1: int) -> str:
    letter = "PCBU"[b0 >> 6]
    return f"{letter}{(b0 >> 4) & 3}{b0 & 0xF:X}{b1 >> 4:X}{b1 & 0xF:X}"


def decode_dtcs(data: bytes, can: bool) -> list[str]:
    """DTC list from a mode 03/07/0A reply body. CAN replies start with a count byte."""
    if can and data:
        data = data[1:]
    codes = []
    for i in range(0, len(data) - 1, 2):
        if data[i] == 0 and data[i + 1] == 0:
            continue
        codes.append(decode_dtc(data[i], data[i + 1]))
    return codes


def describe_dtc(code: str) -> str | None:
    return DTC_DESCRIPTIONS.get(code)


def supported_pids(base: int, data: bytes) -> set[int]:
    """PIDs base+1..base+32 flagged in a 4-byte support bitmap."""
    out = set()
    for n in range(32):
        if n // 8 < len(data) and data[n // 8] & (0x80 >> (n % 8)):
            out.add(base + n + 1)
    return out


def decode_pid(pid: int, data: bytes):
    """(value, unit) for a known PID, or None if the data is too short."""
    p = PIDS[pid]
    if len(data) < p.nbytes:
        return None
    return p.decode(data[:p.nbytes]), p.unit


def is_can(protocol_text: str) -> bool:
    """ATDPN reply such as 'A6' or '6': protocols 6-9 are CAN."""
    t = protocol_text.strip().upper().lstrip("A")
    return t[:1] in ("6", "7", "8", "9")
```

- [ ] **Step 4: Run to verify it passes, then the whole suite**

Run: `.venv/bin/python -m pytest tests/test_obd_protocol.py -q`. Expected: all pass.
Run: `.venv/bin/python -m pytest -q`. Expected: 167 plus the new tests, none failing.

- [ ] **Step 5: Commit**

```bash
git add crank/obd tests/test_obd_protocol.py
git commit -m "feat: OBD-II protocol layer (ELM327 parsing, DTC and PID decoding)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Transport, simulator and reader

**Files:**
- Create: `crank/obd/transport.py`, `crank/obd/reader.py`
- Test: `tests/test_obd_reader.py`

**Interfaces:**
- Consumes: Task 1's `protocol` functions, `PIDS`, `LIVE_PIDS`, `FREEZE_PIDS`, the error classes.
- Produces: `Transport` (`open()`, `send(cmd, timeout) -> str`, `close()`, `sim: bool`); `SimTransport(scenario)`; `SCENARIOS` (healthy, p2119_zero_rpm, many_codes, limited_pcm, silent_adapter, no_vehicle, garbage, drops_mid_read); `ObdReader(transport, timeout=5.0).read() -> ObdReadout`; `ObdReadout` (`codes`, `freeze_frame`, `live: list[LiveValue]`, `mil_on`, `protocol`, `unsupported_modes`, `raw`, `sim`, `read_at`, `lines()`, `to_event_data()`); `LiveValue(pid, name, unit, value)` with `.supported`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_obd_reader.py`:

```python
import pytest

from crank.obd.errors import ObdNoAdapter, ObdProtocolError, ObdTimeout
from crank.obd.reader import ObdReader
from crank.obd.transport import SCENARIOS, SimTransport


def read(name):
    return ObdReader(SimTransport(name)).read()


def live(r):
    return {v.name: v for v in r.live}


def test_healthy_read_has_no_codes_and_live_values():
    r = read("healthy")
    assert r.codes == [] and r.mil_on is False and r.sim is True and r.freeze_frame is None
    assert live(r)["Engine RPM"].value == 750.0
    assert live(r)["Control module voltage"].value == pytest.approx(14.0)
    assert r.protocol == "A6"


def test_stored_code_with_description_mil_and_freeze_frame():
    r = read("p2119_zero_rpm")
    assert [(c["code"], c["status"]) for c in r.codes] == [("P2119", "stored")]
    assert "Throttle" in r.codes[0]["description"] and r.mil_on is True
    assert r.freeze_frame["code"] == "P2119"
    assert {"name": "Engine RPM", "value": 0.0, "unit": "rpm"} in r.freeze_frame["values"]
    assert live(r)["Engine RPM"].value == 0.0                 # a real zero is kept as a zero


def test_codes_carry_their_status_and_multi_frame_replies_work():
    r = read("many_codes")
    got = {(c["code"], c["status"]) for c in r.codes}
    assert got == {("P0335", "stored"), ("P0336", "stored"), ("P0016", "stored"),
                   ("P0340", "pending"), ("P0335", "permanent")}


def test_unsupported_pids_are_unsupported_not_zero():
    r = read("limited_pcm")
    v = live(r)
    assert v["Engine RPM"].supported and not v["Control module voltage"].supported
    assert v["Control module voltage"].value is None and not v["Vehicle speed"].supported


def test_unsupported_modes_are_listed_and_do_not_fail_the_read():
    r = read("limited_pcm")
    assert "02" in r.unsupported_modes and r.freeze_frame is None
    assert [c["code"] for c in r.codes] == ["P2119"]


def test_raw_responses_are_kept_as_evidence():
    r = read("p2119_zero_rpm")
    assert r.raw["03"].startswith("43") and "010C" in r.raw


@pytest.mark.parametrize("name,exc", [("silent_adapter", ObdTimeout), ("no_vehicle", ObdProtocolError),
                                       ("garbage", ObdProtocolError), ("drops_mid_read", ObdTimeout)])
def test_failures_raise_clear_errors(name, exc):
    with pytest.raises(exc):
        read(name)


def test_vehicle_not_answering_mentions_the_key():
    with pytest.raises(ObdProtocolError, match="UNABLE|Unable|did not answer"):
        read("no_vehicle")


def test_transport_is_closed_even_when_the_read_fails():
    t = SimTransport("drops_mid_read")
    closed = []
    t.close = lambda: closed.append(True)
    with pytest.raises(ObdTimeout):
        ObdReader(t).read()
    assert closed


def test_a_missing_adapter_raises_no_adapter():
    sim = SimTransport("healthy")
    sim.sc = type(sim.sc)(fault="no_adapter")
    with pytest.raises(ObdNoAdapter):
        ObdReader(sim).read()


def test_every_scenario_is_known_and_unknown_is_rejected():
    assert {"healthy", "p2119_zero_rpm", "silent_adapter"} <= set(SCENARIOS)
    with pytest.raises(ValueError):
        SimTransport("nope")


def test_readout_lines_are_plain_language():
    lines = read("limited_pcm").lines()
    assert lines[0] == "Codes (simulated): P2119 (stored)" and "Check engine light: on" in lines
    assert any(l.startswith("Live values read: 2 of 10 (not supported:") for l in lines)
    assert any("does not support mode" in l for l in lines)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_obd_reader.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'crank.obd.reader'`.

- [ ] **Step 3: Implement**

Create `crank/obd/transport.py`:

```python
"""Transport interface for an ELM327-style adapter, and a simulated one for tests and demos."""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ObdNoAdapter, ObdTimeout
from .protocol import request


class Transport:
    """One command in, the adapter's reply text out (everything up to the '>' prompt)."""

    sim = False

    def open(self) -> None:
        raise NotImplementedError

    def send(self, cmd: str, timeout: float = 5.0) -> str:
        raise NotImplementedError

    def close(self) -> None:
        pass


def _enc_pid_bitmap(pids: set[int], base: int) -> bytes:
    out = bytearray(4)
    for p in pids:
        n = p - base - 1
        if 0 <= n < 32:
            out[n // 8] |= 0x80 >> (n % 8)
    return bytes(out)


def _dtc_bytes(code: str) -> bytes:
    b0 = ("PCBU".index(code[0]) << 6) | (int(code[1]) << 4) | int(code[2], 16)
    return bytes([b0, int(code[3:5], 16)])


def _frames(msg: bytes) -> str:
    """CAN framing as an ELM327 prints it with headers off: single line, or length + indexed lines."""
    if len(msg) <= 7:
        return msg.hex().upper()
    chunks = [msg[:6]] + [msg[i:i + 7] for i in range(6, len(msg), 7)]
    lines = [f"{len(msg):03X}"] + [f"{i}: {' '.join(f'{b:02X}' for b in c)}" for i, c in enumerate(chunks)]
    return "\r".join(lines)


@dataclass
class Scenario:
    stored: list = field(default_factory=list)
    pending: list = field(default_factory=list)
    permanent: list = field(default_factory=list)
    mil: bool = False
    freeze_dtc: str | None = None
    values: dict = field(default_factory=dict)         # pid -> data bytes (the supported PIDs)
    freeze_values: dict = field(default_factory=dict)
    protocol: str = "A6"
    unsupported_modes: tuple = ()                      # modes the PCM answers with NO DATA / '?'
    fault: str | None = None                           # silent | no_adapter | no_vehicle | garbage | drop_after:N


LIVE_RUNNING = {0x0C: bytes([0x0B, 0xB8]), 0x42: bytes([0x36, 0xB0]), 0x05: bytes([0x82]), 0x0D: bytes([0]),
                0x04: bytes([0x40]), 0x06: bytes([0x80]), 0x07: bytes([0x84]), 0x11: bytes([0x30]),
                0x0F: bytes([0x4B]), 0x0B: bytes([0x23])}
LIVE_CRANKING = {**LIVE_RUNNING, 0x0C: bytes([0, 0]), 0x42: bytes([0x2E, 0xE0])}

SCENARIOS = {
    "healthy": Scenario(values=LIVE_RUNNING),
    "p2119_zero_rpm": Scenario(stored=["P2119"], mil=True, freeze_dtc="P2119", values=LIVE_CRANKING,
                               freeze_values={0x0C: bytes([0, 0]), 0x05: bytes([0x82])}),
    "many_codes": Scenario(stored=["P0335", "P0336", "P0016"], pending=["P0340"], permanent=["P0335"],
                           mil=True, freeze_dtc="P0335", values=LIVE_RUNNING,
                           freeze_values={0x0C: bytes([0x0B, 0xB8])}),
    "limited_pcm": Scenario(stored=["P2119"], mil=True, values={0x0C: bytes([0, 0]), 0x05: bytes([0x82])},
                            unsupported_modes=(0x0A, 0x02)),
    "silent_adapter": Scenario(fault="silent"),
    "no_vehicle": Scenario(fault="no_vehicle"),
    "garbage": Scenario(fault="garbage"),
    "drops_mid_read": Scenario(values=LIVE_RUNNING, fault="drop_after:8"),
}


class SimTransport(Transport):
    sim = True

    def __init__(self, scenario: str = "healthy"):
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown OBD scenario {scenario!r}")
        self.name, self.sc, self.n, self.opened = scenario, SCENARIOS[scenario], 0, False

    def open(self) -> None:
        if self.sc.fault == "no_adapter":
            raise ObdNoAdapter("No OBD adapter found.")
        self.opened = True

    def send(self, cmd: str, timeout: float = 5.0) -> str:
        if not self.opened:
            raise ObdNoAdapter("The adapter is not open.")
        self.n += 1
        f = self.sc.fault
        if f == "silent":
            raise ObdTimeout("The adapter did not answer. Check it is plugged in and powered.")
        if f and f.startswith("drop_after:") and self.n > int(f.split(":")[1]):
            raise ObdTimeout("The adapter stopped answering partway through the read.")
        cmd = cmd.strip().upper().replace(" ", "")
        if cmd.startswith("AT"):
            return self._at(cmd)
        if f == "garbage":
            return "@#$%^ \r\x00\x01"
        if f == "no_vehicle":
            return "UNABLE TO CONNECT"
        return self._obd(cmd)

    def _at(self, cmd: str) -> str:
        if cmd == "ATZ":
            return "ELM327 v1.5"
        if cmd == "ATDPN":
            return self.sc.protocol
        return "OK"

    def _obd(self, cmd: str) -> str:
        sc = self.sc
        mode, rest = int(cmd[:2], 16), cmd[2:]
        if mode in sc.unsupported_modes:
            return bytes([0x7F, mode, 0x11]).hex().upper()
        if mode in (0x03, 0x07, 0x0A):
            codes = {0x03: sc.stored, 0x07: sc.pending, 0x0A: sc.permanent}[mode]
            if not codes:
                return "NO DATA"
            body = bytes([mode + 0x40, len(codes)]) + b"".join(_dtc_bytes(c) for c in codes)
            return _frames(body)
        pid = int(rest[:2], 16) if rest else None
        if mode == 0x01:
            if pid in (0x00, 0x20, 0x40):
                sup = set(sc.values) | {0x01}
                if pid == 0x00 and any(p > 0x20 for p in sup):
                    sup.add(0x20)
                if pid == 0x20 and any(p > 0x40 for p in sup):
                    sup.add(0x40)
                return _frames(bytes([0x41, pid]) + _enc_pid_bitmap(sup, pid))
            if pid == 0x01:
                return _frames(bytes([0x41, 0x01, (0x80 if sc.mil else 0) | len(sc.stored), 0, 0, 0]))
            if pid in sc.values:
                return _frames(bytes([0x41, pid]) + sc.values[pid])
            return "NO DATA"
        if mode == 0x02:
            if pid == 0x02:
                return (_frames(bytes([0x42, 0x02, 0]) + _dtc_bytes(sc.freeze_dtc)) if sc.freeze_dtc
                        else "NO DATA")
            if pid in sc.freeze_values:
                return _frames(bytes([0x42, pid, 0]) + sc.freeze_values[pid])
            return "NO DATA"
        return "?"


__all__ = ["Transport", "SimTransport", "SCENARIOS", "request"]
```

Create `crank/obd/reader.py`:

```python
"""ObdReader: run one full read (codes, freeze frame, live snapshot) over a Transport."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import protocol as P
from .errors import ObdNotSupported, ObdProtocolError
from .pids import FREEZE_PIDS, LIVE_PIDS, PIDS
from .transport import Transport

INIT = ("ATZ", "ATE0", "ATL0", "ATS0", "ATH0", "ATSP0")
STATUS_MODES = (("stored", 0x03), ("pending", 0x07), ("permanent", 0x0A))


@dataclass
class LiveValue:
    pid: int
    name: str
    unit: str
    value: float | None          # None = the PCM does not support / did not answer this PID

    @property
    def supported(self) -> bool:
        return self.value is not None


@dataclass
class ObdReadout:
    codes: list = field(default_factory=list)            # [{"code","status","description"}]
    freeze_frame: dict | None = None                     # {"code","description","values":[LiveValue-like dicts]}
    live: list = field(default_factory=list)             # [LiveValue]
    mil_on: bool | None = None
    protocol: str = ""
    unsupported_modes: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    sim: bool = False
    read_at: str = ""

    def lines(self) -> list[str]:
        """Plain-language result lines for the UI."""
        sim = " (simulated)" if self.sim else ""
        codes = ", ".join(f"{c['code']} ({c['status']})" for c in self.codes)
        out = [f"Codes{sim}: " + (codes or "none stored, pending or permanent")]
        if self.mil_on is not None:
            out.append("Check engine light: " + ("on" if self.mil_on else "off"))
        got = [v for v in self.live if v.supported]
        out.append(f"Live values read: {len(got)} of {len(self.live)}"
                   + (f" (not supported: {', '.join(v.name for v in self.live if not v.supported)})"
                      if len(got) < len(self.live) else ""))
        if self.freeze_frame:
            out.append(f"Freeze frame for {self.freeze_frame['code']}")
        if self.unsupported_modes:
            out.append("PCM does not support mode(s): " + ", ".join(self.unsupported_modes))
        return out

    def to_event_data(self) -> dict:
        return {"codes": self.codes, "freeze_frame": self.freeze_frame, "mil_on": self.mil_on,
                "source": "obd", "sim": self.sim, "protocol": self.protocol,
                "unsupported_modes": self.unsupported_modes, "raw": self.raw}


class ObdReader:
    def __init__(self, transport: Transport, timeout: float = 5.0):
        self.t, self.timeout = transport, timeout
        self.raw: dict = {}

    def _ask(self, cmd: str) -> str:
        resp = self.t.send(cmd, self.timeout)
        self.raw[cmd] = resp.strip()
        return resp

    def _mode_data(self, mode: int, pid: int | None = None, frame: bool = False) -> bytes | None:
        return P.response_data(self._ask(P.request(mode, pid, frame)), mode, pid, frame)

    def read(self) -> ObdReadout:
        out = ObdReadout(sim=self.t.sim, read_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        self.t.open()
        try:
            for cmd in INIT:
                self._ask(cmd)
            first = self._mode_data(0x01, 0x00)           # triggers protocol search
            if first is None:
                raise ObdProtocolError("The vehicle did not answer. Is the key on?")
            out.protocol = self._ask("ATDPN").strip()
            can = P.is_can(out.protocol)
            supported = P.supported_pids(0x00, first)
            for base in (0x20, 0x40):
                if base in supported:
                    data = self._mode_data(0x01, base)
                    supported |= P.supported_pids(base, data) if data else set()
            self._status(out)
            self._codes(out, can)
            self._freeze(out, supported)
            self._live(out, supported)
        finally:
            self.t.close()
        out.raw = self.raw
        return out

    def _status(self, out: ObdReadout) -> None:
        data = self._mode_data(0x01, 0x01)
        out.mil_on = bool(data[0] & 0x80) if data else None

    def _codes(self, out: ObdReadout, can: bool) -> None:
        for status, mode in STATUS_MODES:
            try:
                data = self._mode_data(mode)
            except ObdNotSupported:
                data = None
                out.unsupported_modes.append(f"{mode:02X}")
            for code in P.decode_dtcs(data or b"", can):
                out.codes.append({"code": code, "status": status, "description": P.describe_dtc(code) or ""})

    def _freeze(self, out: ObdReadout, supported: set) -> None:
        try:
            data = self._mode_data(0x02, 0x02, frame=True)
        except ObdNotSupported:
            out.unsupported_modes.append("02")
            return
        if not data or len(data) < 2:
            return
        code = P.decode_dtc(data[0], data[1])
        values = []
        for pid in FREEZE_PIDS:
            try:
                d = self._mode_data(0x02, pid, frame=True)
            except ObdNotSupported:
                d = None
            dec = P.decode_pid(pid, d) if d else None
            if dec:
                values.append({"name": PIDS[pid].name, "value": dec[0], "unit": dec[1]})
        out.freeze_frame = {"code": code, "description": P.describe_dtc(code) or "", "values": values}

    def _live(self, out: ObdReadout, supported: set) -> None:
        for pid in LIVE_PIDS:
            p, value = PIDS[pid], None
            if pid in supported:
                try:
                    d = self._mode_data(0x01, pid)
                except ObdNotSupported:
                    d = None
                dec = P.decode_pid(pid, d) if d else None
                value = dec[0] if dec else None
            out.live.append(LiveValue(pid, p.name, p.unit, value))
```

- [ ] **Step 4: Run to verify it passes, then the whole suite**

Run: `.venv/bin/python -m pytest tests/test_obd_reader.py -q`. Expected: all pass.
Run: `.venv/bin/python -m pytest -q`. Expected: no failures.

- [ ] **Step 5: Commit**

```bash
git add crank/obd tests/test_obd_reader.py
git commit -m "feat: OBD reader over a swappable transport, with a simulator

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Record OBD reads in the case, summary and reports

**Files:**
- Modify: `crank/case.py`, `crank/summary.py`, `crank/render.py`
- Test: `tests/test_obd_case.py`

**Interfaces:**
- Consumes: `ObdReadout` from Task 2 (`to_event_data()`, `.live`, `.sim`).
- Produces: `Case.append_obd(readout) -> list[Event]`; `Summary.freeze_frame`; summary code rows gain `sim`; measurement rows gain `sim`; `_validate` accepts an empty `codes` list only when `data["source"] == "obd"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_obd_case.py`:

```python
import pytest

from crank.case import Case
from crank.obd.reader import ObdReader
from crank.obd.transport import SimTransport
from crank.render import to_html, to_markdown
from crank.store import SessionStore
from crank.summary import build_summary


@pytest.fixture
def case(tmp_path):
    return Case(SessionStore(tmp_path).open("c"))


def read(name):
    return ObdReader(SimTransport(name)).read()


def test_a_read_appends_one_code_read_and_a_measurement_per_supported_value(case):
    r = read("p2119_zero_rpm")
    events = case.append_obd(r)
    assert [e.type for e in events].count("code_read") == 1
    cr = events[0]
    assert cr.data["source"] == "obd" and cr.data["sim"] is True and cr.data["codes"][0]["code"] == "P2119"
    meas = [e for e in events if e.type == "measurement"]
    assert len(meas) == sum(v.supported for v in r.live) and all(e.refs["obd"] == cr.id for e in meas)


def test_unsupported_values_append_no_measurement(case):
    events = case.append_obd(read("limited_pcm"))
    names = {e.data["name"] for e in events if e.type == "measurement"}
    assert names == {"Engine RPM", "Coolant temperature"}


def test_a_clean_read_is_recorded_as_an_empty_code_list(case):
    events = case.append_obd(read("healthy"))
    assert events[0].type == "code_read" and events[0].data["codes"] == []


def test_manual_code_read_still_needs_a_code(case):
    with pytest.raises(ValueError):
        case.append("code_read", "", {"codes": [], "source": "manual"})


def test_zero_rpm_condition_does_not_claim_the_engine_is_off(case):
    events = case.append_obd(read("p2119_zero_rpm"))
    rpm = next(e for e in events if e.data.get("name") == "Engine RPM")
    assert rpm.data["value"] == 0.0 and "engine off" not in rpm.data["conditions"]
    assert "0 RPM" in rpm.data["conditions"]


def test_running_condition_when_rpm_is_nonzero(case):
    events = case.append_obd(read("healthy"))
    assert next(e for e in events if e.data.get("name") == "Engine RPM").data["conditions"] == "OBD, engine running"


def test_summary_carries_status_freeze_frame_and_simulated_flag(case):
    case.append_obd(read("p2119_zero_rpm"))
    s = build_summary(case)
    assert [(c["code"], c["status"], c["sim"]) for c in s.codes] == [("P2119", "stored", True)]
    assert s.freeze_frame["code"] == "P2119" and s.freeze_frame["sim"] is True
    assert all(m["sim"] for m in s.measurements)


def test_a_later_clean_read_marks_earlier_codes_as_not_in_latest_read(case):
    case.append_obd(read("p2119_zero_rpm"))
    case.append_obd(read("healthy"))
    s = build_summary(case)
    assert s.codes[0]["code"] == "P2119" and s.codes[0]["status"] == "not in latest OBD read"


def test_a_code_missing_from_a_later_obd_read_does_not_erase_manual_history(case):
    case.append("code_read", "", {"codes": [{"code": "P0335", "status": "history"}], "source": "manual"})
    case.append_obd(read("p2119_zero_rpm"))
    got = {c["code"]: c["status"] for c in build_summary(case).codes}
    assert got == {"P0335": "not in latest OBD read", "P2119": "stored"}


def test_timeline_lines_for_obd_reads(case):
    case.append_obd(read("healthy"))
    case.append_obd(read("p2119_zero_rpm"))
    lines = [t["line"] for t in build_summary(case).timeline if t["type"] == "code_read"]
    assert lines == ["OBD read [SIMULATED]: no stored, pending or permanent codes",
                     "OBD read [SIMULATED]: P2119 (stored)"]


def test_reports_label_simulated_obd_data_and_show_the_freeze_frame(case):
    case.append_obd(read("p2119_zero_rpm"))
    md = to_markdown(build_summary(case))
    assert "P2119 (stored) [SIMULATED]" in md and "Freeze frame [SIMULATED] for P2119" in md
    assert "Engine RPM: 0 rpm [SIMULATED]" in md
    html = to_html(build_summary(case))
    assert "P2119 (stored) [SIMULATED]" in html and "Freeze frame [SIMULATED]" in html


def test_reports_escape_obd_text(case):
    r = read("p2119_zero_rpm")
    r.codes[0]["description"] = "<script>alert(1)</script>"
    case.append_obd(r)
    assert "<script>" not in to_html(build_summary(case))
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_obd_case.py -q`
Expected: failures with `AttributeError: 'Case' object has no attribute 'append_obd'`.

- [ ] **Step 3: Implement**

Save this patch to `$TMPDIR/task.patch` and apply it from the repo root with `patch -p1 < $TMPDIR/task.patch`:

```diff
--- a/crank/case.py
+++ b/crank/case.py
@@ -120,7 +120,7 @@
             raise ValueError("changing a hypothesis to ruled out or confirmed needs a reason")
     elif type_ in ("symptom", "observation", "change") and not text.strip():
         raise ValueError(f"a {type_} needs some text")
-    elif type_ == "code_read" and not data.get("codes"):
+    elif type_ == "code_read" and not data.get("codes") and data.get("source") != "obd":
         raise ValueError("a code read needs at least one code")
 
 
@@ -195,6 +195,19 @@
         return self.append("capture", label, {"snapshot": capture_snapshot(analysis), "sim": sim},
                            {"capture": capture_id})
 
+    def append_obd(self, readout) -> list[Event]:
+        """Record one OBD read: a code_read (empty list = a clean read) plus a measurement per live value."""
+        out = [self.append("code_read", "", readout.to_event_data())]
+        rpm = next((v.value for v in readout.live if v.name == "Engine RPM"), None)
+        cond = "OBD, engine running" if rpm else "OBD, PCM reports 0 RPM or no RPM"
+        for v in readout.live:
+            if v.value is None:
+                continue
+            out.append(self.append("measurement", "", {
+                "name": v.name, "value": v.value, "unit": v.unit, "where": "OBD live data",
+                "conditions": cond, "sim": readout.sim}, {"obd": out[0].id}))
+        return out
+
     def log_guided_step(self, step, values: dict) -> list[Event]:
         """Turn a saved guided-test step into events."""
         out = []
--- a/crank/summary.py
+++ b/crank/summary.py
@@ -19,7 +19,8 @@
     open: list = field(default_factory=list)            # [{"title", "reason", "evidence"}]
     confirmed: list = field(default_factory=list)
     measurements: list = field(default_factory=list)    # latest per name
-    codes: list = field(default_factory=list)           # active codes from the latest code read
+    codes: list = field(default_factory=list)           # every code read, with its latest status
+    freeze_frame: dict | None = None                    # from the latest OBD read that had one
     captures: list = field(default_factory=list)        # [{"id","label","ts","sim","snapshot"}]
     before_after: list = field(default_factory=list)    # [{"change","before","after"}]
     not_tried: list = field(default_factory=list)       # guided step titles with no data
@@ -51,6 +52,10 @@
         head = (snap["verdict"][0]["title"] if snap and snap.get("verdict") else "verdict " + NOT_RECORDED)
         return f"Capture {e.refs.get('capture', '?')}{sim}: {e.text or 'unlabeled'} — {head}"
     if e.type == "code_read":
+        sim = " [SIMULATED]" if d.get("sim") else ""
+        if d.get("source") == "obd":
+            codes = ", ".join(f"{c.get('code', '?')} ({c.get('status', '?')})" for c in d.get("codes", []))
+            return f"OBD read{sim}: " + (codes or "no stored, pending or permanent codes")
         codes = ", ".join(c.get("code", "?") for c in d.get("codes", []))
         return f"Read codes: {codes}"
     if e.type == "hypothesis":
@@ -79,15 +84,26 @@
             key = (e.data.get("name", ""), e.data.get("where", ""), e.data.get("conditions", ""))
             latest[key] = e
     s.measurements = [dict(name=e.data.get("name", ""), value=e.data["value"], unit=e.data.get("unit", ""),
-                           where=e.data.get("where", ""), conditions=e.data.get("conditions", ""))
+                           where=e.data.get("where", ""), conditions=e.data.get("conditions", ""),
+                           sim=bool(e.data.get("sim")))
                       for e in latest.values()]
 
     codes: dict[str, dict] = {}                       # every code ever read, with its latest status
     for e in events:
-        if e.type == "code_read":
-            for c in e.data.get("codes", []):
-                if isinstance(c, dict) and c.get("code"):
-                    codes[c["code"]] = c
+        if e.type != "code_read":
+            continue
+        rows = [c for c in e.data.get("codes", []) if isinstance(c, dict) and c.get("code")]
+        sim = bool(e.data.get("sim"))
+        if e.data.get("source") == "obd":               # an OBD read lists every code the PCM holds
+            now = {c["code"] for c in rows}
+            for k, c in list(codes.items()):
+                if k not in now:
+                    codes[k] = {**c, "status": "not in latest OBD read"}
+            ff = e.data.get("freeze_frame")
+            if isinstance(ff, dict):
+                s.freeze_frame = {**ff, "sim": sim, "ts": e.ts}
+        for c in rows:
+            codes[c["code"]] = {**c, "sim": sim}
     s.codes = list(codes.values())
 
     caps = [e for e in events if e.type == "capture"]
--- a/crank/render.py
+++ b/crank/render.py
@@ -37,9 +37,22 @@
 
 def _code(c: dict) -> str:
     st_ = f" ({c['status']})" if c.get("status") else ""
-    return f"{c.get('code')}{st_} {c.get('description', '')}".strip()
+    sim = " [SIMULATED]" if c.get("sim") else ""
+    return f"{c.get('code')}{st_}{sim} {c.get('description', '')}".strip()
 
 
+def _measure(m: dict) -> str:
+    sim = " [SIMULATED]" if m.get("sim") else ""
+    return f"{m['name']}: {m['value']:g} {m['unit']}{sim}" + (f" ({m['where']})" if m["where"] else "")
+
+
+def _freeze(ff: dict) -> str:
+    sim = " [SIMULATED]" if ff.get("sim") else ""
+    vals = "; ".join(f"{v['name']} {v['value']:g} {v['unit']}" for v in ff.get("values", []))
+    return (f"Freeze frame{sim} for {ff.get('code')} {ff.get('description', '')}: ".replace("  ", " ")
+            + (vals or NOT_RECORDED))
+
+
 INFERRED = "(inferred from the order of events)"
 
 
@@ -63,7 +76,9 @@
     out += ["", "## Key data"]
     if s.codes:
         out.append("- Codes: " + ", ".join(_code(c) for c in s.codes))
-    out += [f"- {m['name']}: {m['value']:g} {m['unit']}" + (f" ({m['where']})" if m["where"] else "") for m in s.measurements]
+    if s.freeze_frame:
+        out.append("- " + _freeze(s.freeze_frame))
+    out += [f"- {_measure(m)}" for m in s.measurements]
     out += [f"- Capture {_cap_line(c)}" for c in s.captures]
     if not (s.codes or s.measurements or s.captures):
         out.append(f"- {NOT_RECORDED}")
@@ -150,7 +165,9 @@
         data = []
         if s.codes:
             data.append("Codes: " + e(", ".join(_code(c) for c in s.codes)))
-        data += [f"{e(m['name'])}: {m['value']:g} {e(m['unit'])}" for m in s.measurements]
+        if s.freeze_frame:
+            data.append(e(_freeze(s.freeze_frame)))
+        data += [e(_measure(m)) for m in s.measurements]
         body.append("<h2>Key data</h2>" + (_li(data) or f"<p class='muted'>{NOT_RECORDED}</p>"))
         if s.captures:
             body.append('<div class="pair">' + "".join(_cap_html(c, plots.get(c["id"], "")) for c in s.captures) + "</div>")
```

- [ ] **Step 4: Run to verify it passes, then the whole suite**

Run: `.venv/bin/python -m pytest tests/test_obd_case.py -q`. Expected: all pass.
Run: `.venv/bin/python -m pytest -q`. Expected: no failures (existing summary and render tests unchanged).

- [ ] **Step 5: Commit**

```bash
git add crank tests/test_obd_case.py
git commit -m "feat: record OBD reads as case events; show codes, freeze frame and live values in reports

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Controller action and the Case page / phone buttons

**Files:**
- Modify: `crank/controller.py`, `case_ui.py`, `remote.py`, `tests/test_app.py`
- Test: `tests/test_obd_controller.py`, plus three added tests in `tests/test_app.py`

**Interfaces:**
- Consumes: `ObdReader`, `SimTransport`, `SCENARIOS`, `Case.append_obd`, `ObdReadout.lines()`.
- Produces: `Controller.read_obd(scenario="healthy") -> (ObdReadout, None) | (None, str)`; widget keys `obd_scenario`, `obd_read` (Case page), `remote_obd_scenario`, `remote_obd` (phone).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_obd_controller.py`:

```python
import pytest

from crank.controller import Controller
from crank.store import SessionStore


def test_controller_read_records_and_returns_the_readout(tmp_path):
    c = Controller(SessionStore(tmp_path))
    r, err = c.read_obd("p2119_zero_rpm")
    assert err is None and r.codes[0]["code"] == "P2119"
    assert [e.type for e in c.case().events()].count("code_read") == 1


@pytest.mark.parametrize("name", ["silent_adapter", "no_vehicle", "garbage", "drops_mid_read"])
def test_controller_failure_returns_a_message_and_appends_nothing(tmp_path, name):
    c = Controller(SessionStore(tmp_path))
    r, err = c.read_obd(name)
    assert r is None and err and c.case().events() == []
```

Apply this patch to `tests/test_app.py` (it adds three tests at the end):

Save this patch to `$TMPDIR/task.patch` and apply it from the repo root with `patch -p1 < $TMPDIR/task.patch`:

```diff
--- a/tests/test_app.py
+++ b/tests/test_app.py
@@ -298,3 +298,26 @@
     _case().append("observation", "new info")
     at.run()
     assert any("out of date" in w.value for w in at.warning)
+
+
+def test_case_page_obd_read_records_codes_and_shows_a_result(at):
+    at.selectbox(key="obd_scenario").select("p2119_zero_rpm").run()
+    at.button(key="obd_read").click().run()
+    assert not at.exception
+    assert any("P2119 (stored)" in m.value for m in at.markdown)
+    assert [e.type for e in get_controller().case().events()].count("code_read") == 1
+
+
+def test_case_page_obd_failure_shows_an_error_and_records_nothing(at):
+    at.selectbox(key="obd_scenario").select("silent_adapter").run()
+    at.button(key="obd_read").click().run()
+    assert not at.exception and at.error
+    assert get_controller().case().events() == []
+
+
+def test_phone_remote_obd_button(at):
+    at.query_params["page"] = "remote"
+    at.run()
+    at.button(key="remote_obd").click().run()
+    assert not at.exception
+    assert any("Codes (simulated)" in m.value for m in at.markdown)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_obd_controller.py tests/test_app.py -q`
Expected: `AttributeError: 'Controller' object has no attribute 'read_obd'` and the three app tests failing on missing widget keys.

- [ ] **Step 3: Implement**

Save this patch to `$TMPDIR/task.patch` and apply it from the repo root with `patch -p1 < $TMPDIR/task.patch`:

```diff
--- a/crank/controller.py
+++ b/crank/controller.py
@@ -8,6 +8,9 @@
 from .capture import CaptureJob
 from .case import Case
 from .model import CaptureSettings, Thresholds
+from .obd.errors import ObdError
+from .obd.reader import ObdReader
+from .obd.transport import SimTransport
 from .scope import RigolScope, SimScope
 from .store import SessionStore
 
@@ -62,6 +65,17 @@
     def case(self) -> Case:
         return Case(self.store.open(self.session_name))
 
+    def read_obd(self, scenario: str = "healthy"):
+        """One OBD read into the current case. Returns (readout, None) or (None, message).
+
+        Only the simulator exists so far; a real adapter plugs in as another Transport."""
+        try:
+            readout = ObdReader(SimTransport(scenario)).read()
+        except ObdError as e:
+            return None, str(e)
+        self.case().append_obd(readout)
+        return readout, None
+
     def cancel(self) -> None:
         if self.job:
             self.job.cancel()
--- a/case_ui.py
+++ b/case_ui.py
@@ -3,6 +3,8 @@
 
 import streamlit as st
 
+from crank.controller import get_controller
+from crank.obd.transport import SCENARIOS
 from crank.case import EVENT_TYPES, HYPOTHESIS_STATUSES, Case, Vehicle, suggest_hypotheses
 from crank.render import capture_svg, to_html, to_markdown
 from crank.summary import build_summary
@@ -69,6 +71,21 @@
             st.error(str(e))
 
 
+def _obd(case):
+    st.subheader("Read from the vehicle (OBD-II)")
+    st.caption("Simulator only until an adapter is connected. A read adds the codes, freeze frame and "
+               "live values to the case; it never clears codes.")
+    scen = st.selectbox("Simulated vehicle", list(SCENARIOS), key="obd_scenario")
+    if st.button("Read from OBD", key="obd_read"):
+        readout, err = get_controller().read_obd(scen)
+        if err:
+            st.error(err)
+        else:
+            st.success("Recorded in the case:")
+            for line in readout.lines():
+                st.write("- " + line)
+
+
 def _board(case, last_codes):
     st.subheader("Suspects")
     hyps = case.hypotheses()
@@ -153,6 +170,7 @@
     _vehicle(case)
     cap_ids = [c["id"] for c in case.session.list_captures()]
     _quick_add(case, cap_ids)
+    _obd(case)
     snaps = [e.data["snapshot"] for e in events if e.type == "capture" and e.data.get("snapshot")]
     codes = [v["code"] for v in snaps[-1]["verdict"]] if snaps else []
     _board(case, codes)
--- a/remote.py
+++ b/remote.py
@@ -37,6 +37,17 @@
                 except ValueError as e:
                     st.error(str(e))
 
+    from crank.obd.transport import SCENARIOS
+    with st.expander("Read OBD (simulated)"):
+        scen = st.selectbox("Simulated vehicle", list(SCENARIOS), key="remote_obd_scenario")
+        if st.button("Read OBD", key="remote_obd", use_container_width=True):
+            readout, err = ctrl.read_obd(scen)
+            if err:
+                st.error(err)
+            else:
+                for line in readout.lines():
+                    st.write("- " + line)
+
     state = snap["state"]
     if snap["running"]:
         if state in ("armed", "triggered"):
```

- [ ] **Step 4: Run to verify it passes, then the whole suite**

Run: `.venv/bin/python -m pytest tests/test_obd_controller.py tests/test_app.py -q`. Expected: all pass.
Run: `.venv/bin/python -m pytest -q`. Expected: no failures.

- [ ] **Step 5: Commit**

```bash
git add crank case_ui.py remote.py tests
git commit -m "feat: Read from OBD on the Case page and the phone remote

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: README

**Files:**
- Modify: `README.md`

- [ ] **Step 1:** In the Pages list, at the end of the **Case:** bullet, add: ` It also has **Read from OBD**, which records codes, freeze frame and live values from the vehicle.`

- [ ] **Step 2:** Add this section after "### Phone remote" and before "## First session at the truck":

```markdown
### Reading the vehicle (OBD-II)

**Read from OBD** on the Case page (and **Read OBD** on the phone) asks the vehicle for its stored, pending and
permanent trouble codes, the freeze frame, and a one-shot snapshot of live values (RPM, battery voltage, coolant
temperature, fuel trims and a few more). Each read is added to the case: one `code_read` event plus a measurement for
every value the PCM answered. A value the PCM does not support is listed as not supported, never as zero, and a
real 0 RPM is kept as 0. The tool never clears codes.

Only the simulator is wired up so far, with eight scenarios (healthy, a stored P2119 with 0 RPM, many codes,
a PCM with limited support, and four failure modes). The adapter layer is a small `Transport` interface
(`open`, `send`, `close`), so a classic Bluetooth serial adapter (via `pyserial`) or a BLE adapter (via `bleak`)
can be added without touching the protocol code.
```

- [ ] **Step 3:** Add this bullet to "Not verified against real hardware yet": `- The OBD layer: ELM327 clone quirks and timing, protocol auto-detection on this vehicle, and which modes and PIDs the PSI-flashed E38 answers.`

- [ ] **Step 4:** In "Code layout", after the `crank/summary.py` line, add: `    crank/obd/           OBD-II: protocol (pure parsing), pids, transport (interface and simulator), reader`

- [ ] **Step 5: Run the suite and commit**

Run: `.venv/bin/python -m pytest -q`. Expected: no failures.

```bash
git add README.md
git commit -m "docs: README section on reading the vehicle over OBD-II

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Self-review

- Spec coverage: protocol and PIDs (Task 1); transport, simulator, reader, failure scenarios (Task 2); events, summary, renderers, simulated labels, empty-read semantics (Task 3); controller and both buttons (Task 4); docs (Task 5). Out-of-scope items (clear codes, serial and BLE transports, streaming) have no task, by design.
- Placeholders: none; all code is in the tasks.
- Types: `ObdReadout`, `LiveValue`, `Transport.send`, `Case.append_obd`, `Controller.read_obd` match across tasks.
- Deviations from the first spec draft were folded back into the spec: `send` instead of write/read, synchronous read, empty `code_read` for clean reads, negative responses, "not in latest OBD read".
