# Multi-ECU OBD Reads Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attribute every OBD answer to the ECU that sent it, record trouble codes from all ECUs tagged with their ECU, and take live values, freeze frame and supported PIDs from the PCM only.

**Architecture:** The reader switches the adapter to headers-on (`ATH1`). A new pure parser in `protocol.py` reassembles raw 11-bit CAN / ISO-TP frames per ECU. `SimTransport` learns header-on framing and extra ECUs. `summary.py` keys codes by `(code, ecu)`; ECU labels show only when a read had more than one ECU.

**Tech Stack:** Python 3.11, pytest.

**Spec:** `docs/superpowers/specs/2026-10-12-multi-ecu-reads-design.md` (builds on `2026-10-12-serial-obd-transport-design.md`, PR #12; this branch is stacked on `feature/issue-4-serial-transport`).

## Global Constraints

- Only 11-bit CAN is supported: a response header must be three hex digits starting with `7` (`7E8`–`7EF`); anything else (29-bit CAN, J1850, ISO 9141) raises `ObdProtocolError` with a plain message.
- `ATH1` is already on the allowlist; the reader must send no command outside it (the slice-1 drift test enforces this).
- A single-ECU read produces the same codes, wording, report text and summary rows as today; no ECU label appears unless more than one ECU answered.
- Old case events without an `ecu` field keep working.
- Existing `messages()`, `response_data()` and their tests are unchanged.
- Never invent a value; a PID the PCM does not answer stays "not supported".

## Review Focus

- Frames from two ECUs interleaved, in either order (Task 1).
- Multi-frame reply with a missing or out-of-order consecutive frame: error, never a truncated message (Task 1).
- Reply text without spaces (`ATS0`, what the reader sets) and with spaces both parse (Task 1).
- Same code reported by two ECUs stays two rows through summary and reports (Task 4).
- PCM answers on a header other than `7E8`, or an ECU rejects a mode: the read still completes and says so (Task 3).

## File Structure

- Modify `crank/obd/protocol.py` — `ecu_messages`, `response_data_by_ecu`, `primary_ecu`, shared adapter-error check.
- Modify `crank/obd/transport.py` — header-on framing, `other_ecus`, new scenarios.
- Modify `crank/obd/reader.py` — `ATH1`, per-ECU logic, readout fields.
- Modify `crank/summary.py`, `crank/render.py` — `(code, ecu)` keys and labels.
- Modify `README.md`.
- Tests: `tests/test_obd_protocol.py`, `tests/test_obd_reader.py`, `tests/test_obd_case.py` (extend); `tests/test_obd_transport.py` (create).

---

### Task 1: Per-ECU protocol parsing

**Files:**
- Modify: `crank/obd/protocol.py`
- Test: `tests/test_obd_protocol.py`

**Interfaces:**
- Produces:
  - `ecu_messages(resp: str) -> dict[str, bytes] | None` (header → reassembled message; `None` for NO DATA).
  - `response_data_by_ecu(resp: str, mode: int, pid: int | None = None, frame: bool = False) -> tuple[dict[str, bytes], list[str]] | None` — `(data_by_ecu, not_supported_ecus)`; `None` for NO DATA.
  - `primary_ecu(headers) -> str`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_obd_protocol.py`)

```python
def test_ecu_single_frame_with_and_without_spaces():
    want = {"7E8": bytes.fromhex("4100BE3FA813")}
    assert P.ecu_messages("7E8 06 41 00 BE 3F A8 13\r>") == want
    assert P.ecu_messages("7E8064100BE3FA813\r\r>") == want


def test_ecu_single_frame_padding_is_stripped():
    assert P.ecu_messages("7E8 02 41 00 AA AA AA AA AA") == {"7E8": bytes.fromhex("4100")}


def test_ecu_short_single_frame_is_an_error():
    with pytest.raises(ObdProtocolError):
        P.ecu_messages("7E8 06 41 00")


def test_ecu_multi_frame_is_reassembled_and_truncated():
    resp = "7E8 10 0A 43 03 01 33 02 36\r7E8 21 00 16 00 00 00 00 00\r>"
    assert P.ecu_messages(resp) == {"7E8": bytes.fromhex("43030133023600160000")}


def test_two_ecus_interleaved_in_either_order():
    a = ["7E8 10 0A 43 03 01 33 02 36", "7E8 21 00 16 00 00 00 00 00"]
    b = ["7E9 10 08 43 02 07 00 07 01", "7E9 21 07 02 00 00 00 00 00"]
    want = {"7E8": bytes.fromhex("43030133023600160000"), "7E9": bytes.fromhex("430207000701" "0702")}
    assert P.ecu_messages("\r".join([a[0], b[0], a[1], b[1]])) == want
    assert P.ecu_messages("\r".join([b[0], a[0], b[1], a[1]])) == want


def test_missing_or_out_of_order_consecutive_frame_is_an_error():
    with pytest.raises(ObdProtocolError, match="7E8"):
        P.ecu_messages("7E8 10 0A 43 03 01 33 02 36")
    with pytest.raises(ObdProtocolError, match="7E8"):
        P.ecu_messages("7E8 10 0A 43 03 01 33 02 36\r7E8 22 00 16 00 00 00 00 00")
    with pytest.raises(ObdProtocolError, match="7E8"):
        P.ecu_messages("7E8 21 00 16 00 00 00 00 00")


def test_ecu_no_data_is_none_and_adapter_errors_raise():
    assert P.ecu_messages("NO DATA\r>") is None
    with pytest.raises(ObdProtocolError):
        P.ecu_messages("CAN ERROR")
    with pytest.raises(ObdProtocolError):
        P.ecu_messages("?")


@pytest.mark.parametrize("text", ["18DAF110064100BE3FA8", "48 6B 10 41 0C 1A F8 3C", "@#$%^"])
def test_unsupported_header_shapes_are_rejected_readably(text):
    with pytest.raises(ObdProtocolError, match="11-bit CAN|Unreadable"):
        P.ecu_messages(text)


def test_by_ecu_returns_positive_data_and_lists_not_supported_ecus():
    resp = "7E8 03 7F 03 11 00 00 00 00\r7E9 04 43 01 01 33 00 00 00"
    assert P.response_data_by_ecu(resp, 3) == ({"7E9": bytes([0x01, 0x01, 0x33])}, ["7E8"])


def test_by_ecu_busy_negative_response_names_the_ecu():
    with pytest.raises(ObdProtocolError, match="7E8"):
        P.response_data_by_ecu("7E8 03 7F 03 22 00 00 00 00", 3)


def test_by_ecu_no_data_and_wrong_reply():
    assert P.response_data_by_ecu("NO DATA", 3) is None
    with pytest.raises(ObdProtocolError):
        P.response_data_by_ecu("7E8 04 41 0C 1A F8 00 00 00", 1, 0x0D)


def test_primary_ecu_is_the_lowest_header():
    assert P.primary_ecu(["7E9", "7E8"]) == "7E8"
    assert P.primary_ecu({"7EA": b"", "7E9": b""}) == "7E9"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_obd_protocol.py -q`
Expected: FAIL (AttributeError: `ecu_messages`).

- [ ] **Step 3: Implement.** In `crank/obd/protocol.py`, extract the adapter-error loop from `messages()` into a helper and use it from both parsers:

```python
def _raise_adapter_errors(lines: list[str]) -> None:
    for line in lines:
        if line == "?":
            raise ObdProtocolError("The adapter did not understand the command.")
        if any(line.startswith(e) for e in ERRORS):
            raise ObdProtocolError(f"The adapter reported: {line.title()}.")
```

In `messages()`, replace its `for line in lines:` error loop with `_raise_adapter_errors(lines)`. Then add:

```python
_HEADER_HELP = ("Unsupported reply format from the adapter: only 11-bit CAN (headers 7E8-7EF) is supported "
                "(got {line!r}).")


def ecu_messages(resp: str) -> dict[str, bytes] | None:
    """Headers-on CAN text -> {ecu header: reassembled message}. None means 'NO DATA'.

    Each line is one raw 11-bit CAN frame (three hex digits of header, then up to 8 data bytes, spaces
    optional). ISO-TP single, first and consecutive frames are reassembled per ECU, so replies from
    several ECUs may interleave."""
    lines = _lines(resp)
    if not lines or lines == ["NO DATA"]:
        return None
    _raise_adapter_errors(lines)
    done: dict[str, bytes] = {}
    partial: dict[str, dict] = {}                     # ecu -> {"want", "buf", "next"}
    for line in lines:
        flat = line.replace(" ", "")
        if not _HEX.match(flat):
            raise ObdProtocolError(f"Unreadable response from the adapter: {line[:24]!r}.")
        if not flat.startswith("7"):
            raise ObdProtocolError(_HEADER_HELP.format(line=line[:24]))
        if len(flat) % 2 == 0 or not 5 <= len(flat) <= 19:      # 3 header digits + 1 to 8 whole bytes
            raise ObdProtocolError(f"Unreadable response from the adapter: {line[:24]!r}.")
        ecu, data = flat[:3], bytes.fromhex(flat[3:])
        kind = data[0] >> 4
        if kind == 0:
            n = data[0] & 0xF
            if n == 0 or len(data) - 1 < n:
                raise ObdProtocolError(f"Incomplete reply from {ecu}.")
            done.setdefault(ecu, data[1:1 + n])
        elif kind == 1:
            if len(data) < 3:
                raise ObdProtocolError(f"Incomplete reply from {ecu}.")
            partial[ecu] = {"want": ((data[0] & 0xF) << 8) | data[1], "buf": bytearray(data[2:]), "next": 1}
        elif kind == 2:
            p = partial.get(ecu)
            if p is None or (data[0] & 0xF) != p["next"] % 16:
                raise ObdProtocolError(f"Incomplete reply from {ecu}: a frame is missing or out of order.")
            p["buf"] += data[1:]
            p["next"] += 1
            if len(p["buf"]) >= p["want"]:
                done.setdefault(ecu, bytes(p["buf"][:p["want"]]))
                del partial[ecu]
        else:
            raise ObdProtocolError(f"Unexpected frame type from {ecu}: {data[0]:02X}.")
    if partial:
        raise ObdProtocolError(f"Incomplete reply from {sorted(partial)[0]}: a frame is missing.")
    return done


def response_data_by_ecu(resp: str, mode: int, pid: int | None = None, frame: bool = False):
    """({ecu: data bytes answering this request}, [ecus that said 'not supported']), or None for NO DATA."""
    msgs = ecu_messages(resp)
    if msgs is None:
        return None
    head = bytes([mode + 0x40] + ([pid] if pid is not None else []) + ([0] if frame else []))
    data, unsupported = {}, []
    for ecu, m in msgs.items():
        if m.startswith(head):
            data[ecu] = m[len(head):]
        elif len(m) >= 3 and m[0] == 0x7F and m[1] == mode:
            if m[2] in _NOT_SUPPORTED_NRC:
                unsupported.append(ecu)
            else:
                what = _NRC_TEXT.get(m[2], "an error")
                raise ObdProtocolError(f"{ecu} answered '{what}' (negative response {m[2]:02X}) to mode "
                                       f"{mode:02X}; try again.")
        else:
            raise ObdProtocolError(f"Unexpected reply from {ecu} to mode {mode:02X}: {m.hex().upper()}.")
    return data, unsupported


def primary_ecu(headers) -> str:
    """The PCM: the lowest responding header (normally 7E8)."""
    return min(headers)
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_obd_protocol.py -q`
Expected: PASS (new and existing).

- [ ] **Step 5: Commit**

```bash
git add crank/obd/protocol.py tests/test_obd_protocol.py
git commit -m "Parse per-ECU CAN/ISO-TP replies with headers on

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Simulator with header-on framing and extra ECUs

**Files:**
- Modify: `crank/obd/transport.py`
- Test: `tests/test_obd_transport.py` (create)

**Interfaces:**
- Consumes: `protocol.ecu_messages` (tests only).
- Produces: `Scenario` gains `pcm_header: str = "7E8"`, `other_ecus: dict = {}` (header → `{"stored": [...], "pending": [...], "permanent": [...], "unsupported_modes": (modes,)}`), `interleave: bool = False`. New scenarios `pcm_and_trans`, `interleaved_multiframe`, `same_code_two_ecus`, `pcm_not_7e8`, `trans_rejects_mode`. `SimTransport` honors `ATH1`/`ATH0`/`ATZ`: headers-on replies are raw frames, no spaces. Header-off output is unchanged.

- [ ] **Step 1: Write the failing tests** (`tests/test_obd_transport.py`)

```python
from crank.obd import protocol as P
from crank.obd.transport import SimTransport


def sim(name, headers=True):
    t = SimTransport(name)
    t.open()
    t.send("ATZ")
    t.send("ATH1" if headers else "ATH0")
    return t


def test_headers_off_output_is_unchanged():
    t = sim("p2119_zero_rpm", headers=False)
    assert t.send("010C") == "410C0000"
    assert t.send("03") == "43012119"


def test_headers_on_single_ecu_reply_parses_to_the_pcm():
    t = sim("p2119_zero_rpm")
    assert P.ecu_messages(t.send("010C")) == {"7E8": bytes([0x41, 0x0C, 0, 0])}


def test_atz_resets_headers_to_off():
    t = sim("p2119_zero_rpm")
    t.send("ATZ")
    assert not t.send("010C").startswith("7E8")


def test_pcm_and_trans_both_answer_mode_03_only():
    t = sim("pcm_and_trans")
    msgs = P.ecu_messages(t.send("03"))
    assert set(msgs) == {"7E8", "7E9"} and msgs["7E9"][:2] == bytes([0x43, 0x01])
    assert set(P.ecu_messages(t.send("0100"))) == {"7E8"}


def test_interleaved_multiframe_frames_really_interleave():
    t = sim("interleaved_multiframe")
    lines = t.send("03").split("\r")
    assert [l[:3] for l in lines[:2]] == ["7E8", "7E9"]
    msgs = P.ecu_messages("\r".join(lines))
    assert len(msgs["7E8"]) == 8 and len(msgs["7E9"]) == 8


def test_rejecting_ecu_sends_a_negative_response():
    t = sim("trans_rejects_mode")
    data, unsupported = P.response_data_by_ecu(t.send("0A"), 0x0A)
    assert unsupported == ["7E9"]


def test_pcm_header_can_move():
    t = sim("pcm_not_7e8")
    assert set(P.ecu_messages(t.send("0100"))) == {"7E9"}
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_obd_transport.py -q`
Expected: FAIL (unknown scenario / headers not modeled).

- [ ] **Step 3: Implement** in `crank/obd/transport.py`:

Add `from itertools import zip_longest` at the top. Extend `Scenario`:

```python
    pcm_header: str = "7E8"
    other_ecus: dict = field(default_factory=dict)     # header -> {"stored","pending","permanent","unsupported_modes"}
    interleave: bool = False
```

Add the ISO-TP framer next to `_frames`:

```python
def _isotp(msg: bytes) -> list[bytes]:
    """Raw 8-byte CAN data fields for one message: a single frame, or first + consecutive frames."""
    if len(msg) <= 7:
        return [(bytes([len(msg)]) + msg).ljust(8, b"\x00")]
    out = [bytes([0x10 | (len(msg) >> 8), len(msg) & 0xFF]) + msg[:6]]
    for i, off in enumerate(range(6, len(msg), 7), start=1):
        out.append((bytes([0x20 | (i & 0xF)]) + msg[off:off + 7]).ljust(8, b"\x00"))
    return out
```

Add scenarios to `SCENARIOS`:

```python
    "pcm_and_trans": Scenario(stored=["P2119"], mil=True, freeze_dtc="P2119", values=LIVE_CRANKING,
                              freeze_values={0x0C: bytes([0, 0]), 0x05: bytes([0x82])},
                              other_ecus={"7E9": {"stored": ["P0700"]}}),
    "interleaved_multiframe": Scenario(stored=["P0335", "P0336", "P0016"], values=LIVE_RUNNING, interleave=True,
                                       other_ecus={"7E9": {"stored": ["P0700", "P0701", "P0702"]}}),
    "same_code_two_ecus": Scenario(stored=["P0335"], values=LIVE_RUNNING,
                                   other_ecus={"7E9": {"stored": ["P0335"]}}),
    "pcm_not_7e8": Scenario(stored=["P2119"], values=LIVE_RUNNING, pcm_header="7E9"),
    "trans_rejects_mode": Scenario(stored=["P2119"], values=LIVE_RUNNING,
                                   other_ecus={"7E9": {"stored": ["P0700"], "unsupported_modes": (0x0A,)}}),
```

In `SimTransport.__init__` add `self.headers = False`. In `_at`: `ATZ` sets `self.headers = False`; `ATH1` sets it True; `ATH0` sets False; both return `"OK"` (ATZ keeps returning `"ELM327 v1.5"`).

Refactor `_obd`: rename the existing body to `_pcm_body(mode, pid)` returning `bytes` (the message, where it currently returns `_frames(x)` return `x`), `None` where it returns `"NO DATA"`, and `"?"` for unknown modes; the negative-response branch returns `bytes([0x7F, mode, 0x11])`. Then:

```python
    def _obd(self, cmd: str) -> str:
        sc = self.sc
        mode, rest = int(cmd[:2], 16), cmd[2:]
        pid = int(rest[:2], 16) if rest else None
        body = self._pcm_body(mode, pid)
        if body == "?":
            return "?"
        items = [(sc.pcm_header, body)] if body is not None else []
        if mode in (0x03, 0x07, 0x0A):
            for ecu, o in sc.other_ecus.items():
                b = self._other_body(o, mode)
                if b is not None:
                    items.append((ecu, b))
        if not items:
            return "NO DATA"
        if not self.headers:
            return _frames(items[0][1])
        per = [[ecu + fr.hex().upper() for fr in _isotp(body)] for ecu, body in items]
        order = ([x for group in zip_longest(*per) for x in group if x] if sc.interleave
                 else [x for group in per for x in group])
        return "\r".join(order)

    @staticmethod
    def _other_body(o: dict, mode: int) -> bytes | None:
        if mode in tuple(o.get("unsupported_modes", ())):
            return bytes([0x7F, mode, 0x11])
        codes = o.get({0x03: "stored", 0x07: "pending", 0x0A: "permanent"}[mode], [])
        if not codes:
            return None
        return bytes([mode + 0x40, len(codes)]) + b"".join(_dtc_bytes(c) for c in codes)
```

(Keep the unsupported-mode check in `_pcm_body` as is; `_frames` is still used for header-off output.)

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_obd_transport.py tests/test_obd_reader.py tests/test_obd_allowlist.py -q`
Expected: new tests PASS; existing reader tests still PASS (the reader still sends `ATH0`).

- [ ] **Step 5: Commit**

```bash
git add crank/obd/transport.py tests/test_obd_transport.py
git commit -m "Simulate headers-on CAN framing and extra ECUs

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Reader attributes answers to ECUs

**Files:**
- Modify: `crank/obd/reader.py`
- Test: `tests/test_obd_reader.py`

**Interfaces:**
- Consumes: `P.response_data_by_ecu`, `P.primary_ecu`, new scenarios.
- Produces: `ObdReadout` fields `ecus: list[str]`, `primary_ecu: str`, `notes: list[str]` (all in `to_event_data()`); code rows `{"code","status","description","ecu"}`; non-PCM mode rejections appear in `unsupported_modes` as `"0A (7E9)"`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_obd_reader.py`)

```python
def test_pcm_and_trans_records_both_ecus_codes_tagged():
    r = read("pcm_and_trans")
    assert {(c["code"], c["ecu"]) for c in r.codes} == {("P2119", "7E8"), ("P0700", "7E9")}
    assert r.ecus == ["7E8", "7E9"] and r.primary_ecu == "7E8" and r.notes == []
    assert r.freeze_frame["code"] == "P2119" and live(r)["Engine RPM"].value == 0.0
    assert "P0700 (stored, 7E9)" in " ".join(r.lines())


def test_interleaved_multiframe_code_lists_are_attributed():
    r = read("interleaved_multiframe")
    assert {c["code"] for c in r.codes if c["ecu"] == "7E8"} == {"P0335", "P0336", "P0016"}
    assert {c["code"] for c in r.codes if c["ecu"] == "7E9"} == {"P0700", "P0701", "P0702"}


def test_same_code_from_two_ecus_is_two_rows():
    r = read("same_code_two_ecus")
    assert sorted((c["code"], c["ecu"]) for c in r.codes) == [("P0335", "7E8"), ("P0335", "7E9")]


def test_pcm_not_on_7e8_is_used_and_noted():
    r = read("pcm_not_7e8")
    assert r.primary_ecu == "7E9" and r.codes[0]["ecu"] == "7E9"
    assert any("7E9" in n for n in r.notes) and live(r)["Engine RPM"].supported


def test_an_ecu_rejecting_a_mode_does_not_fail_the_read():
    r = read("trans_rejects_mode")
    assert "0A (7E9)" in r.unsupported_modes and "0A" not in r.unsupported_modes
    assert {c["code"] for c in r.codes} == {"P2119", "P0700"}


def test_single_ecu_read_looks_exactly_as_before():
    r = read("p2119_zero_rpm")
    assert r.ecus == ["7E8"] and r.codes[0]["ecu"] == "7E8"
    assert not any("7E8" in line for line in r.lines())
    assert r.lines()[0] == "Codes (simulated): P2119 (stored)"
```

Also change the existing `test_raw_responses_are_kept_as_evidence` assertion `r.raw["03"].startswith("43")` to `"7E8" in r.raw["03"]` (headers are now part of the raw text).

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_obd_reader.py -q`
Expected: FAIL (`ecus` attribute / `ecu` key missing).

- [ ] **Step 3: Implement** in `crank/obd/reader.py`:

- `INIT = ("ATZ", "ATE0", "ATL0", "ATS0", "ATH1", "ATSP0")`.
- `ObdReadout` gains `ecus: list = field(default_factory=list)`, `primary_ecu: str = ""`, `notes: list = field(default_factory=list)`; `to_event_data()` adds `"ecus": self.ecus, "primary_ecu": self.primary_ecu, "notes": self.notes`.
- `lines()`: build each code as `f"{c['code']} ({c['status']}{', ' + c['ecu'] if multi and c.get('ecu') else ''})"` with `multi = len(self.ecus) > 1`; when `multi`, add the line `"ECUs that answered: " + ", ".join(self.ecus)`; append `self.notes` lines at the end.
- Replace `_mode_data` use with a PCM-only helper and add a by-ECU helper:

```python
    def _by_ecu(self, mode, pid=None, frame=False):
        return P.response_data_by_ecu(self._ask(P.request(mode, pid, frame)), mode, pid, frame)

    def _pcm_data(self, mode, pid=None, frame=False) -> bytes | None:
        res = self._by_ecu(mode, pid, frame)
        if res is None:
            return None
        data, unsupported = res
        if self.primary in unsupported:
            raise ObdNotSupported(f"The PCM does not support mode {mode:02X}.")
        return data.get(self.primary)
```

- In `read()` after `self.t.open()` and INIT: send `0100` with `first_raw = self._ask(P.request(1, 0x00))`, `msgs = P.ecu_messages(first_raw)`; `None` → `ObdProtocolError("The vehicle did not answer. Is the key on?")`. Then `out.protocol = self._ask("ATDPN").strip()`; if `not P.is_can(out.protocol)` raise `ObdProtocolError("Only CAN vehicles are supported.")`. Set `self.primary = P.primary_ecu(msgs)`, `out.primary_ecu = self.primary`, `out.ecus = sorted(msgs)`. If `self.primary != "7E8"`: `out.notes.append(f"The engine controller answered from {self.primary}, not 7E8.")`. Parse the first response's PCM data with `P.response_data_by_ecu(first_raw, 1, 0)[0][self.primary]` for the supported-PID bitmap, then the `0x20`/`0x40` loop via `self._pcm_data(0x01, base)`. Add `self.primary = ""` to `__init__`.
- `_status`, `_freeze`, `_live` call `_pcm_data` instead of `_mode_data` (their existing `ObdNotSupported` handling is unchanged).
- `_codes`:

```python
    def _codes(self, out, can):
        for status, mode in STATUS_MODES:
            res = self._by_ecu(mode)
            if res is None:
                continue
            data, unsupported = res
            for ecu in unsupported:
                out.unsupported_modes.append(f"{mode:02X}" if ecu == self.primary else f"{mode:02X} ({ecu})")
            for ecu, body in data.items():
                if ecu not in out.ecus:
                    out.ecus = sorted(out.ecus + [ecu])
                for code in P.decode_dtcs(body, can):
                    out.codes.append({"code": code, "status": status, "ecu": ecu,
                                      "description": P.describe_dtc(code) or ""})
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_obd_reader.py tests/test_obd_allowlist.py tests/test_obd_serial.py tests/test_obd_controller.py -q`
Expected: PASS, including the allowlist drift test (the reader now sends `ATH1`, which is allowed). If an existing test fails only because a code row now has an `ecu` key or the raw text has headers, update that assertion; do not weaken the behavior it checks.

- [ ] **Step 5: Commit**

```bash
git add crank/obd/reader.py tests/test_obd_reader.py
git commit -m "Attribute OBD answers to ECUs and tag codes with them

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Summary and reports keep ECUs apart

**Files:**
- Modify: `crank/summary.py:56-66,99-116`, `crank/render.py:43-46`
- Test: `tests/test_obd_case.py`

**Interfaces:**
- Consumes: code rows with optional `ecu`; event data `ecus`.
- Produces: `Summary.codes` rows keyed by `(code, ecu)`, each with `"show_ecu": bool`; `render._code` shows `(status, ecu)` when `show_ecu`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_obd_case.py`)

```python
def test_same_code_from_two_ecus_stays_two_rows_in_reports(case):
    case.append_obd(read("same_code_two_ecus"))
    s = build_summary(case)
    assert sorted((c["code"], c["ecu"]) for c in s.codes) == [("P0335", "7E8"), ("P0335", "7E9")]
    md = to_markdown(s)
    assert "P0335 (stored, 7E8)" in md and "P0335 (stored, 7E9)" in md
    html = to_html(s)
    assert "P0335 (stored, 7E8)" in html and "P0335 (stored, 7E9)" in html


def test_timeline_line_labels_ecus_only_when_several_answered(case):
    case.append_obd(read("pcm_and_trans"))
    case.append_obd(read("p2119_zero_rpm"))
    lines = [t["line"] for t in build_summary(case).timeline if t["type"] == "code_read"]
    assert "P0700 (stored, 7E9)" in lines[0] and "P2119 (stored, 7E8)" in lines[0]
    assert "7E8" not in lines[1] and "P2119 (stored)" in lines[1]


def test_single_ecu_report_has_no_ecu_label(case):
    case.append_obd(read("p2119_zero_rpm"))
    assert "P2119 (stored) [SIMULATED]" in to_markdown(build_summary(case))


def test_a_code_missing_from_a_later_read_is_demoted_per_ecu(case):
    case.append_obd(read("pcm_and_trans"))
    case.append_obd(read("p2119_zero_rpm"))              # transmission silent this time
    status = {c["ecu"]: c["status"] for c in build_summary(case).codes}
    assert status["7E9"] == "not in latest OBD read"


def test_old_events_without_an_ecu_still_render(case):
    case.append("code_read", "", {"source": "obd", "sim": True, "codes": [
        {"code": "P0335", "status": "stored", "description": ""}]})
    s = build_summary(case)
    assert s.codes[0]["code"] == "P0335" and "P0335 (stored)" in to_markdown(s)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_obd_case.py -q`
Expected: FAIL (codes keyed by code only; no labels).

- [ ] **Step 3: Implement.**

`crank/summary.py`, timeline line (inside the `source == "obd"` branch):

```python
            multi = len(d.get("ecus") or []) > 1
            codes = ", ".join(
                f"{c.get('code', '?')} ({c.get('status', '?')}"
                + (f", {c['ecu']}" if multi and c.get("ecu") else "") + ")" for c in d.get("codes", []))
```

Code table (replace the `codes` dict logic):

```python
    codes: dict[tuple, dict] = {}                     # every (code, ecu) ever read, with its latest status
    for e in events:
        if e.type != "code_read":
            continue
        rows = [c for c in e.data.get("codes", []) if isinstance(c, dict) and c.get("code")]
        sim = bool(e.data.get("sim"))
        multi = len(e.data.get("ecus") or []) > 1
        if e.data.get("source") == "obd":               # an OBD read lists every code the PCM holds
            now = {(c["code"], c.get("ecu", "")) for c in rows}
            unread = set(e.data.get("unsupported_modes") or [])
            for k, c in list(codes.items()):
                if k not in now and _MODE_OF_STATUS.get(c.get("status")) not in unread:
                    codes[k] = {**c, "status": "not in latest OBD read"}
            ...  # freeze-frame block unchanged
        for c in rows:
            codes[(c["code"], c.get("ecu", ""))] = {**c, "sim": sim, "show_ecu": multi}
    s.codes = list(codes.values())
```

`crank/render.py`:

```python
def _code(c: dict) -> str:
    ecu = f", {c['ecu']}" if c.get("show_ecu") and c.get("ecu") else ""
    st_ = f" ({c['status']}{ecu})" if c.get("status") else ""
```

(rest unchanged).

Known limitation, ledger it as a ruling: a code first recorded before this change has `ecu=""`, so the same code read afterwards (with `ecu="7E8"`) appears as a new row and the old row shows "not in latest OBD read". Only pre-existing real-adapter data is affected, and none exists yet (the simulator was the only source).

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest -q`
Expected: full suite PASS.

- [ ] **Step 5: Commit**

```bash
git add crank/summary.py crank/render.py tests/test_obd_case.py
git commit -m "Keep codes from different ECUs apart in summaries and reports

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Docs

**Files:**
- Modify: `README.md` (the "Reading the vehicle" section and the "OBD layer" open-questions bullet)

- [ ] **Step 1:** In the "Reading the vehicle (OBD-II)" section, replace the sentence telling the reader to wait for multi-ECU handling with: reads now keep every ECU's answers apart; trouble codes from all ECUs are recorded and labeled with the ECU that reported them (shown when more than one ECU answered); live values, the freeze frame and the supported-PID map come from the engine controller (normally `7E8`) only; only 11-bit CAN is supported. Update the scenario count/description to include the multi-ECU simulator scenarios (`pcm_and_trans`, `interleaved_multiframe`, `same_code_two_ecus`, `pcm_not_7e8`, `trans_rejects_mode`).
- [ ] **Step 2:** In the open-questions bullet that says the first answer wins, remove "Before the first real read the reader must also handle several ECUs ... the first answer wins." and keep the remaining hardware-validation text.
- [ ] **Step 3:** Run `python -m pytest -q`. Expected: PASS.
- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "Document multi-ECU reads

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```
