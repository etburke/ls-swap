# Adapter Identification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every real OBD read records which adapter produced it (ELM version, STN firmware, device, serial), and the reader knows whether ST features are available, without changing how any read behaves.

**Architecture:** A new pure module `crank/obd/adapter.py` holds `AdapterInfo` and the reply parsers. The allowlist gains `ATI`, `STI`, `STDI`, `STSN` and nothing else under `ST*`. `ObdReader` runs a best-effort `_identify()` step after `INIT` and stores the result in `ObdReadout.adapter`, which flows into the code_read event data, the result lines, and one line in the case summary and report (real reads only). The simulator gains three adapter presets.

**Tech Stack:** Python 3, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-15-adapter-identification-design.md`

## Global Constraints

- Only these new commands may be sent: `ATI`, `STI`, `STDI`, `STSN`. Every other `ST*` command and `ATPP` stays refused by the allowlist.
- Identification is best-effort: an `ObdTimeout` or `ObdAdapterError` on an ID command adds a note and the read continues. `ObdNoAdapter` (and any other error) still propagates. ID commands are never retried.
- Identification runs once per read, after `INIT` and before the first `0100`. An `LV RESET` re-init does not repeat it.
- A reply of the shape `STN<digits> v<digits>.<digits>[.<digits>]` to `STI` means STN; anything else (`?`, `NO DATA`, junk, an echo) means not STN.
- Nothing branches on `supports_st` in this slice.
- The case summary and report show the adapter line only for the latest non-simulated OBD read that has an `adapter` dict. Simulated reads never set it. Events recorded before this slice render exactly as before.
- Do not use `_lines()` from `protocol.py` to clean ID replies: its `NOISE` list drops lines starting with `ELM327` and `OK`, which would erase the `ATI` reply.
- Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Test command for the whole suite: `python -m pytest -q`.

## Review Focus

- An `ATI` reply (`ELM327 v1.5`) must survive cleaning; the existing line cleaner would delete it (pinned in Task 1 and Task 4).
- A clone that answers `STI` with junk instead of `?` is treated as not STN, with no error and no note (Task 4).
- A timeout on one ID command leaves the earlier results in place and the read completes (Task 4).
- A dead adapter (`ObdNoAdapter`) during identification still fails the read rather than being noted away (Task 4).
- A case file written before this slice, with no `adapter` key, still summarises and renders (Task 5).
- A simulated read after a real one must not replace the real adapter line (Task 5).

---

### Task 1: AdapterInfo and reply parsing

**Files:**
- Create: `crank/obd/adapter.py`
- Test: `tests/test_obd_adapter.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `AdapterInfo(elm_version="", stn_version="", device="", serial="", notes=[])` dataclass.
  - `AdapterInfo.supports_st -> bool` (property), `.label() -> str`, `.to_dict() -> dict`, `AdapterInfo.from_dict(d) -> AdapterInfo` (classmethod, tolerates `None`, non-dicts and missing keys).
  - `parse_sti(text: str) -> str`: the matched version string, or `""`.
  - `clean_id(text: str, cmd: str = "") -> str`: first usable line of an ID reply, or `""`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_obd_adapter.py`:

```python
import pytest

from crank.obd.adapter import AdapterInfo, clean_id, parse_sti


@pytest.mark.parametrize("text,want", [
    ("STN2255 v4.3.1", "STN2255 v4.3.1"),
    ("\rSTN2255 v4.3.1\r\r", "STN2255 v4.3.1"),
    ("STI\rSTN1110 v2.2.0\r", "STN1110 v2.2.0"),
    ("STN2255 v4.3", "STN2255 v4.3"),
])
def test_parse_sti_accepts_an_stn_version(text, want):
    assert parse_sti(text) == want


@pytest.mark.parametrize("text", ["?", "NO DATA", "STI", "ELM327 v1.5", "@#$%^ \r\x00\x01", "", "STN v1", "OK"])
def test_parse_sti_rejects_everything_else(text):
    assert parse_sti(text) == ""


def test_clean_id_keeps_an_elm_version_line():
    assert clean_id("ELM327 v1.5\r\r") == "ELM327 v1.5"       # the line cleaner in protocol.py would drop this


def test_clean_id_drops_an_echo_blank_lines_and_the_unknown_marker():
    assert clean_id("ATI\rELM327 v1.5\r", "ATI") == "ELM327 v1.5"
    assert clean_id("?\r") == ""
    assert clean_id("") == ""
    assert clean_id("  \r\r") == ""


def test_label_for_an_stn_adapter():
    a = AdapterInfo(elm_version="ELM327 v1.5", stn_version="STN2255 v4.3.1", device="OBDLink MX+", serial="123456")
    assert a.supports_st and a.label() == "OBDLink MX+ (STN2255 v4.3.1, serial 123456)"


def test_label_leaves_out_what_is_unknown():
    assert AdapterInfo(stn_version="STN2255 v4.3.1").label() == "STN adapter (STN2255 v4.3.1)"
    assert AdapterInfo(stn_version="STN2255 v4.3.1", device="OBDLink MX+").label() == "OBDLink MX+ (STN2255 v4.3.1)"


def test_label_for_a_plain_elm327():
    a = AdapterInfo(elm_version="ELM327 v1.5")
    assert not a.supports_st and a.label() == "ELM327 v1.5 (not an STN adapter, no ST features)"


def test_label_when_nothing_was_identified():
    assert AdapterInfo().label() == "unidentified" and not AdapterInfo().supports_st


def test_dict_round_trip():
    a = AdapterInfo("ELM327 v1.5", "STN2255 v4.3.1", "OBDLink MX+", "123456", ["STSN timed out"])
    assert AdapterInfo.from_dict(a.to_dict()) == a


@pytest.mark.parametrize("d", [None, {}, "junk", {"stn_version": None, "notes": "x"}, {"unknown": 1}])
def test_from_dict_tolerates_missing_and_odd_input(d):
    a = AdapterInfo.from_dict(d)
    assert a == AdapterInfo() and a.notes == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_obd_adapter.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'crank.obd.adapter'`.

- [ ] **Step 3: Write the implementation**

Create `crank/obd/adapter.py`:

```python
"""What the adapter says it is. Pure: parsing and labelling only, no I/O."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_STN = re.compile(r"STN\d+\s+v\d+(?:\.\d+)+", re.IGNORECASE)


def clean_id(text: str, cmd: str = "") -> str:
    """The first usable line of an ID reply. Drops the prompt, blank lines, an echo of `cmd`, and the
    adapter's '?' for a command it does not know."""
    for raw in re.split(r"[\r\n]+", text.replace(">", "")):
        line = raw.strip()
        if line and line != "?" and line.upper() != cmd.strip().upper():
            return line
    return ""


def parse_sti(text: str) -> str:
    """The STN firmware string (for example 'STN2255 v4.3.1') when the reply has that shape, else ''.
    An ELM327 clone answers '?', and a bad one may answer anything, so only the shape counts."""
    m = _STN.search(text)
    return m.group(0) if m else ""


@dataclass
class AdapterInfo:
    elm_version: str = ""                  # ATI, for example "ELM327 v1.5"
    stn_version: str = ""                  # STI, for example "STN2255 v4.3.1"; empty on a plain ELM327
    device: str = ""                       # STDI, for example "OBDLink MX+"
    serial: str = ""                       # STSN
    notes: list = field(default_factory=list)

    @property
    def supports_st(self) -> bool:
        """The gate for ST features: only a reply of STN shape counts."""
        return bool(self.stn_version)

    def label(self) -> str:
        if self.supports_st:
            parts = [self.stn_version] + ([f"serial {self.serial}"] if self.serial else [])
            return f"{self.device or 'STN adapter'} ({', '.join(parts)})"
        if self.elm_version:
            return f"{self.elm_version} (not an STN adapter, no ST features)"
        return "unidentified"

    def to_dict(self) -> dict:
        return {"elm_version": self.elm_version, "stn_version": self.stn_version, "device": self.device,
                "serial": self.serial, "notes": list(self.notes)}

    @classmethod
    def from_dict(cls, d) -> "AdapterInfo":
        d = d if isinstance(d, dict) else {}

        def s(key: str) -> str:
            return str(d.get(key) or "")
        notes = d.get("notes")
        return cls(s("elm_version"), s("stn_version"), s("device"), s("serial"),
                   [str(n) for n in notes] if isinstance(notes, list) else [])
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_obd_adapter.py -q`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Commit**

```bash
git add crank/obd/adapter.py tests/test_obd_adapter.py
git commit -m "Add AdapterInfo and ID reply parsing

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Allowlist and adapter-error helper

**Files:**
- Modify: `crank/obd/allowlist.py`
- Modify: `crank/obd/protocol.py` (add `raise_if_adapter_error` after `_raise_adapter_errors`)
- Test: `tests/test_obd_allowlist.py`, `tests/test_obd_protocol.py`

**Interfaces:**
- Consumes: `ObdAdapterError` from `errors.py` (existing).
- Produces:
  - `allowlist.check()` now accepts `ATI`, `STI`, `STDI`, `STSN`.
  - `protocol.raise_if_adapter_error(resp: str) -> None`: raises `ObdAdapterError` if any line of `resp` is a known adapter error; ignores `?` lines (an ID command an adapter does not know is not an error); never raises for anything else.

- [ ] **Step 1: Write the failing tests**

In `tests/test_obd_allowlist.py`, remove `"STI"` from the parametrized list in `test_forbidden_commands_are_rejected`, and add `"STPTO 100"`, `"STIX"`, `"STDIX"`, `"STSNX"`, `"STSN 1"` to it. Add after `test_allowed_commands_are_normalized`:

```python
@pytest.mark.parametrize("cmd,norm", [("ATI", "ATI"), ("ati", "ATI"), ("STI", "STI"), ("sti", "STI"),
                                      ("ST DI", "STDI"), (" stsn ", "STSN")])
def test_identification_commands_are_allowed(cmd, norm):
    assert check(cmd) == norm
```

In `tests/test_obd_protocol.py` add (import `raise_if_adapter_error` from `crank.obd.protocol` alongside the existing imports from that module):

```python
@pytest.mark.parametrize("text,kind", [("CAN ERROR", "CAN ERROR"), ("\rLV RESET\r", "LV RESET"),
                                       ("BUFFER FULL", "BUFFER FULL")])
def test_raise_if_adapter_error_raises_the_specific_error(text, kind):
    with pytest.raises(ObdAdapterError) as e:
        raise_if_adapter_error(text)
    assert e.value.kind == kind


@pytest.mark.parametrize("text", ["?", "STN2255 v4.3.1", "ELM327 v1.5", "OBDLink MX+ 1234", "OK", "", "NO DATA"])
def test_raise_if_adapter_error_ignores_ordinary_replies(text):
    raise_if_adapter_error(text)
```

If `ObdAdapterError` is not yet imported in `tests/test_obd_protocol.py`, import it from `crank.obd.errors`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_obd_allowlist.py tests/test_obd_protocol.py -q`
Expected: FAIL: the new allowlist cases are refused (`STI` etc.), and `ImportError: cannot import name 'raise_if_adapter_error'`.

- [ ] **Step 3: Write the implementation**

In `crank/obd/allowlist.py`, change the docstring's first lines and sets:

```python
"""Read-only gate for everything sent to a real adapter. An allowlist, not a denylist:
anything not listed (ATPP, every ST* command except the three identification ones, mode 04/09, ...)
is refused."""
```

```python
AT_ALLOWED = frozenset({"ATZ", "ATE0", "ATL0", "ATS0", "ATH0", "ATH1", "ATSP0", "ATDPN", "ATDP", "ATI"})
ST_ALLOWED = frozenset({"STI", "STDI", "STSN"})        # identify the adapter; nothing else under ST*
```

and in `check`:

```python
    if norm in AT_ALLOWED or norm in ST_ALLOWED or any(p.fullmatch(norm) for p in _OBD):
```

In `crank/obd/protocol.py`, directly after `_raise_adapter_errors`:

```python
def raise_if_adapter_error(resp: str) -> None:
    """Raise ObdAdapterError if the reply is a known adapter fault. A '?' line is ignored: for an ID
    command it only means the adapter does not know it."""
    _raise_adapter_errors([line for line in _lines(resp) if line != "?"])
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_obd_allowlist.py tests/test_obd_protocol.py -q`
Expected: PASS. (The drift test `test_every_command_the_reader_sends_passes` still passes: the reader does not send the new commands yet.)

- [ ] **Step 5: Commit**

```bash
git add crank/obd/allowlist.py crank/obd/protocol.py tests/test_obd_allowlist.py tests/test_obd_protocol.py
git commit -m "Allow the adapter ID commands and nothing else under ST

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Simulator adapter presets

**Files:**
- Modify: `crank/obd/transport.py`
- Test: `tests/test_obd_transport.py`

**Interfaces:**
- Consumes: `ObdTimeout` (already imported in `transport.py`).
- Produces:
  - `ADAPTERS: dict[str, dict]` mapping preset name (`"stn"`, `"elm327"`, `"flaky_id"`) to `{command: reply}`; a reply of `None` means the command times out; a command missing from the preset answers `?`.
  - `Scenario.adapter: str = "stn"`.
  - `SimTransport` answers `ATI`, `STI`, `STDI`, `STSN` from the preset.
  - New scenarios `elm_clone` (`adapter="elm327"`) and `flaky_adapter_id` (`adapter="flaky_id"`).
  - The `drops_mid_read` fault moves from `drop_after:8` to `drop_after:12` so it still drops at the same point of the read (four ID commands now precede the first request).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_obd_transport.py` (it already imports `pytest`, `SimTransport` and `SCENARIOS`; add `ObdTimeout` from `crank.obd.errors` and `ADAPTERS` from `crank.obd.transport` if missing):

```python
def _open(name):
    t = SimTransport(name)
    t.open()
    return t


def test_the_stn_preset_answers_every_id_command():
    t = _open("healthy")
    assert t.send("ATI") == "ELM327 v1.5"
    assert t.send("STI") == "STN2255 v4.3.1"
    assert t.send("STDI") == "OBDLink MX+ 1234"
    assert t.send("STSN") == "110012345678"


def test_a_clone_answers_question_marks_to_st_commands():
    t = _open("elm_clone")
    assert t.send("ATI") == "ELM327 v1.5"
    assert t.send("STI") == "?" and t.send("STDI") == "?" and t.send("STSN") == "?"


def test_the_flaky_preset_times_out_on_the_serial_number_only():
    t = _open("flaky_adapter_id")
    assert t.send("STI") == "STN2255 v4.3.1"
    with pytest.raises(ObdTimeout):
        t.send("STSN")


def test_every_scenario_names_a_known_adapter_preset():
    assert {s.adapter for s in SCENARIOS.values()} <= set(ADAPTERS)
    assert "elm_clone" in SCENARIOS and "flaky_adapter_id" in SCENARIOS
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_obd_transport.py -q`
Expected: FAIL: `ImportError: cannot import name 'ADAPTERS'` (or `Scenario` has no `adapter`).

- [ ] **Step 3: Write the implementation**

In `crank/obd/transport.py`, add the presets above `class Scenario`:

```python
ADAPTERS = {                                           # what an adapter answers to the ID commands
    "stn": {"ATI": "ELM327 v1.5", "STI": "STN2255 v4.3.1", "STDI": "OBDLink MX+ 1234", "STSN": "110012345678"},
    "elm327": {"ATI": "ELM327 v1.5"},                  # a clone: every ST* command answers '?'
    "flaky_id": {"ATI": "ELM327 v1.5", "STI": "STN2255 v4.3.1", "STDI": "OBDLink MX+ 1234", "STSN": None},
}
```

Add to `Scenario`, after `error_times`:

```python
    adapter: str = "stn"                               # key into ADAPTERS; None reply = the command times out
```

Add two scenarios to `SCENARIOS` (after `buffer_full`) and change the drop fault:

```python
    "elm_clone": Scenario(values=LIVE_RUNNING, adapter="elm327"),
    "flaky_adapter_id": Scenario(values=LIVE_RUNNING, adapter="flaky_id"),
```

```python
    "drops_mid_read": Scenario(values=LIVE_RUNNING, fault="drop_after:12"),
```

In `SimTransport.send`, directly before `if cmd.startswith("AT"):` add:

```python
        if cmd in ("STI", "STDI", "STSN"):
            return self._identify(cmd)
```

Add the method after `send`:

```python
    def _identify(self, cmd: str) -> str:
        ids = ADAPTERS[self.sc.adapter]
        if cmd not in ids:
            return "?"
        if ids[cmd] is None:
            raise ObdTimeout("The adapter did not answer. Check it is plugged in and powered.")
        return ids[cmd]
```

In `_at`, before `if cmd == "ATDPN":` add:

```python
        if cmd == "ATI":
            return ADAPTERS[self.sc.adapter]["ATI"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_obd_transport.py -q`
Expected: PASS.

Then run the whole suite: `python -m pytest -q`
Expected: PASS (the reader does not send the new commands yet, so nothing else moves; the `drops_mid_read` tests still raise `ObdTimeout`).

- [ ] **Step 5: Commit**

```bash
git add crank/obd/transport.py tests/test_obd_transport.py
git commit -m "Simulate STN, ELM327 clone and flaky adapter identities

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Reader identifies the adapter

**Files:**
- Modify: `crank/obd/reader.py`
- Test: `tests/test_obd_reader.py`

**Interfaces:**
- Consumes: `AdapterInfo`, `clean_id`, `parse_sti` (Task 1); `P.raise_if_adapter_error` (Task 2); `ADAPTERS`, `elm_clone`, `flaky_adapter_id` (Task 3).
- Produces:
  - `ObdReadout.adapter: AdapterInfo` (default empty); `to_event_data()["adapter"]` is `adapter.to_dict()`.
  - `ObdReadout.lines()` has `Adapter: <label>` (or `Adapter (simulated): <label>`) as its second line.
  - `ObdReader._identify() -> AdapterInfo`; its notes are copied into `ObdReadout.notes`; the replies are kept in `raw` under `ATI`, `STI`, `STDI`, `STSN`.

- [ ] **Step 1: Write the failing tests**

Add to the imports of `tests/test_obd_reader.py`: `from crank.obd.errors import ObdNoAdapter` is already there. Append:

```python
def test_a_genuine_stn_adapter_is_identified_and_recorded():
    r = read("healthy")
    a = r.adapter
    assert a.supports_st and a.elm_version == "ELM327 v1.5" and a.stn_version == "STN2255 v4.3.1"
    assert a.device == "OBDLink MX+ 1234" and a.serial == "110012345678"
    assert r.notes == []                                       # a clean identification leaves no note
    assert r.to_event_data()["adapter"]["stn_version"] == "STN2255 v4.3.1"
    assert r.raw["STI"] == "STN2255 v4.3.1" and r.raw["ATI"] == "ELM327 v1.5"


def test_an_elm327_clone_is_identified_as_plain_and_the_read_succeeds():
    t = sent_by(SimTransport("elm_clone"))
    r = ObdReader(t, retry_pause=0).read()
    assert not r.adapter.supports_st and r.adapter.elm_version == "ELM327 v1.5"
    assert r.adapter.device == "" and r.adapter.serial == "" and r.notes == []
    assert "STDI" not in t.sent and "STSN" not in t.sent       # no ST command beyond the one that proved it
    assert "not an STN adapter" in r.adapter.label()
    assert live(r)["Engine RPM"].value == 750.0


def test_a_clone_that_answers_sti_with_junk_is_not_stn_and_draws_no_note():
    class Junk(SimTransport):
        def send(self, cmd, timeout=5.0):
            return "@#$%^ \r\x00\x01" if cmd == "STI" else super().send(cmd, timeout)

    r = ObdReader(Junk("healthy"), retry_pause=0).read()
    assert not r.adapter.supports_st and r.notes == []


def test_a_timeout_on_one_id_command_is_noted_and_the_read_completes():
    r = read("flaky_adapter_id")
    assert r.adapter.supports_st and r.adapter.device == "OBDLink MX+ 1234" and r.adapter.serial == ""
    assert any("STSN" in n and "timed out" in n for n in r.notes)
    assert r.adapter.notes and r.adapter.notes[0] in r.notes
    assert live(r)["Engine RPM"].value == 750.0


def test_an_adapter_error_on_an_id_command_is_noted_not_fatal():
    class Busy(SimTransport):
        def send(self, cmd, timeout=5.0):
            return "BUS BUSY" if cmd == "STDI" else super().send(cmd, timeout)

    r = ObdReader(Busy("healthy"), retry_pause=0).read()
    assert r.adapter.device == "" and r.adapter.serial == "110012345678"
    assert any("STDI" in n and "BUS BUSY" in n for n in r.notes)


def test_a_missing_adapter_during_identification_still_fails_the_read():
    class Gone(SimTransport):
        def send(self, cmd, timeout=5.0):
            if cmd == "STI":
                raise ObdNoAdapter("The adapter connection dropped.")
            return super().send(cmd, timeout)

    with pytest.raises(ObdNoAdapter):
        ObdReader(Gone("healthy"), retry_pause=0).read()


def test_identification_runs_once_after_init_and_before_the_first_request():
    t = sent_by(SimTransport("lv_reset_mid_read"))             # an LV RESET re-runs INIT mid-read
    ObdReader(t, retry_pause=0).read()
    assert t.sent.count("STI") == 1 and t.sent.count("ATI") == 1
    assert t.sent.index("ATI") > t.sent.index("ATSP0") and t.sent.index("STI") < t.sent.index("0100")


def test_result_lines_show_the_adapter_second():
    lines = read("healthy").lines()
    assert lines[1] == "Adapter (simulated): OBDLink MX+ 1234 (STN2255 v4.3.1, serial 110012345678)"
    assert read("elm_clone").lines()[1].startswith("Adapter (simulated): ELM327 v1.5 (not an STN adapter")
```

and add this helper near the top, after `live`:

```python
def sent_by(t):
    """Wrap a SimTransport so every command it is sent is kept in t.sent."""
    t.sent = []
    real = t.send

    def send(cmd, timeout=5.0):
        t.sent.append(cmd)
        return real(cmd, timeout)
    t.send = send
    return t
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_obd_reader.py -q`
Expected: FAIL: `AttributeError: 'ObdReadout' object has no attribute 'adapter'` and similar.

- [ ] **Step 3: Write the implementation**

In `crank/obd/reader.py`:

Imports:

```python
from . import protocol as P
from .adapter import AdapterInfo, clean_id, parse_sti
from .errors import ObdAdapterError, ObdNotSupported, ObdProtocolError, ObdTimeout
```

In `ObdReadout`, after `notes`:

```python
    adapter: AdapterInfo = field(default_factory=AdapterInfo)
```

In `lines()`, replace `out = [f"Codes{sim}: " + codes]` with:

```python
        out = [f"Codes{sim}: " + codes, f"Adapter{sim}: {self.adapter.label()}"]
```

In `to_event_data()`, add `"adapter": self.adapter.to_dict()` to the returned dict.

In `ObdReader`, after `_request` add:

```python
    def _id(self, cmd: str, info: AdapterInfo) -> str:
        """One ID command's cleaned reply. '' when the adapter does not know it or it fails (noted)."""
        try:
            resp = self._ask(cmd)
            P.raise_if_adapter_error(resp)
        except ObdTimeout:
            info.notes.append(f"Adapter identification incomplete: {cmd} timed out.")
            return ""
        except ObdAdapterError as e:
            info.notes.append(f"Adapter identification incomplete: {cmd} drew {e.kind}.")
            return ""
        return clean_id(resp, cmd)

    def _identify(self) -> AdapterInfo:
        """Who is on the other end? Provenance, so best-effort: a failed ID command is noted, never fatal.
        The ST commands are only sent once STI has answered with an STN version."""
        info = AdapterInfo()
        info.elm_version = self._id("ATI", info)
        info.stn_version = parse_sti(self._id("STI", info))
        if info.stn_version:
            info.device = self._id("STDI", info)
            info.serial = self._id("STSN", info)
        return info
```

In `read()`, directly after the `for cmd in INIT:` loop:

```python
            out.adapter = self._identify()
            out.notes.extend(out.adapter.notes)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_obd_reader.py -q`
Expected: PASS.

Then the whole suite: `python -m pytest -q`
Expected: PASS, including `test_every_command_the_reader_sends_passes` for every scenario (it now covers `ATI`, `STI`, `STDI`, `STSN`) and the existing `drops_mid_read` tests. If an existing test fails because it asserted the exact list of result lines or the exact commands sent, update only that assertion for the new second line or the four new commands, and record the change as a ruling in the ledger.

- [ ] **Step 5: Commit**

```bash
git add crank/obd/reader.py tests/test_obd_reader.py
git commit -m "Identify the adapter at the start of every read

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Adapter line in the case summary and report

**Files:**
- Modify: `crank/summary.py`
- Modify: `crank/render.py`
- Test: `tests/test_obd_case.py`

**Interfaces:**
- Consumes: `AdapterInfo.from_dict(...).label()` (Task 1); the code_read event's `adapter` dict (Task 4).
- Produces:
  - `Summary.adapter: str = ""`: the label of the latest non-simulated OBD read that has an `adapter` dict.
  - `to_markdown` and `to_html` show `Adapter: <label>` once under Key data, after the codes line, when `Summary.adapter` is set.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_obd_case.py`:

```python
STN = {"elm_version": "ELM327 v1.5", "stn_version": "STN2255 v4.3.1", "device": "OBDLink MX+",
       "serial": "123456", "notes": []}
STN_LINE = "Adapter: OBDLink MX+ (STN2255 v4.3.1, serial 123456)"


def real_read(case, adapter):
    data = {"source": "obd", "sim": False, "codes": [], "ecus": ["7E8"]}
    if adapter is not None:
        data["adapter"] = adapter
    case.append("code_read", "", data)


def test_a_real_reads_adapter_shows_once_in_the_summary_and_both_reports(case):
    real_read(case, STN)
    s = build_summary(case)
    assert s.adapter == "OBDLink MX+ (STN2255 v4.3.1, serial 123456)"
    assert to_markdown(s).count(STN_LINE) == 1 and to_html(s).count(STN_LINE) == 1


def test_a_simulated_read_never_sets_the_adapter_line(case):
    case.append_obd(read("healthy"))                       # the simulator pretends to be an STN
    s = build_summary(case)
    assert s.adapter == "" and "Adapter:" not in to_markdown(s) and "Adapter:" not in to_html(s)


def test_a_simulated_read_after_a_real_one_does_not_replace_it(case):
    real_read(case, STN)
    case.append_obd(read("elm_clone"))
    assert build_summary(case).adapter.startswith("OBDLink MX+")


def test_the_latest_real_read_wins(case):
    real_read(case, STN)
    real_read(case, {"elm_version": "ELM327 v1.5"})
    assert build_summary(case).adapter == "ELM327 v1.5 (not an STN adapter, no ST features)"


def test_events_recorded_before_adapter_identification_still_render(case):
    real_read(case, None)
    s = build_summary(case)
    assert s.adapter == "" and "Adapter:" not in to_markdown(s) and "Adapter:" not in to_html(s)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_obd_case.py -q`
Expected: FAIL: `AttributeError: 'Summary' object has no attribute 'adapter'`.

- [ ] **Step 3: Write the implementation**

In `crank/summary.py`:

Add the import after `from .case import ...`:

```python
from .obd.adapter import AdapterInfo
```

Add to `Summary` after `freeze_frame`:

```python
    adapter: str = ""                                   # label of the adapter on the latest real OBD read
```

In `build_summary`'s `code_read` loop, directly after `multi = ...`:

```python
        if e.data.get("source") == "obd" and not sim and isinstance(e.data.get("adapter"), dict):
            s.adapter = AdapterInfo.from_dict(e.data["adapter"]).label()
```

In `crank/render.py`, in `to_markdown`, after the codes line:

```python
    if s.codes:
        out.append("- Codes: " + _md(", ".join(_code(c) for c in s.codes)))
    if s.adapter:
        out.append("- Adapter: " + _md(s.adapter))
```

and in `to_html`, after the codes entry:

```python
        if s.codes:
            data.append("Codes: " + e(", ".join(_code(c) for c in s.codes)))
        if s.adapter:
            data.append("Adapter: " + e(s.adapter))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_obd_case.py -q`
Expected: PASS.

Then the whole suite: `python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add crank/summary.py crank/render.py tests/test_obd_case.py
git commit -m "Show the adapter on the case summary and report

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 6: README

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: the behaviour from Tasks 1 to 5.
- Produces: documentation only.

- [ ] **Step 1: Find the OBD section**

Run: `grep -n "allowlist\|ATZ\|timeout\|OBD" README.md | head -30`
Expected: the section that documents the serial transport, the read-only allowlist and the adapter timing and error handling added in #15.

- [ ] **Step 2: Add the documentation**

In that section, next to the allowlist description, add (matching the surrounding style and heading level):

```markdown
**Adapter identification.** At the start of every read the reader asks the adapter who it is: `ATI` (the ELM327 version) and `STI`. If `STI` answers with an STN firmware string such as `STN2255 v4.3.1`, it also asks `STDI` (device name) and `STSN` (serial number). A plain ELM327 clone answers `?` to `STI` and is recorded as "not an STN adapter, no ST features". The result is stored in the case's code_read event, shown in the Read OBD result lines, and printed once in the case summary and report for real (not simulated) reads. Identification never fails a read: a timeout or adapter error on an ID command is noted and the read carries on. These four are the only identification commands on the allowlist; every other `ST*` command is still refused. Nothing yet changes behaviour based on what was found; later work (`STPTO`, `STPX`) will use it. Not yet checked against a real adapter (#3).
```

- [ ] **Step 3: Run the whole suite**

Run: `python -m pytest -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "Document adapter identification

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```
