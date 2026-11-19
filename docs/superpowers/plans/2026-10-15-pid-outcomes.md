# PID Outcomes and Report Presentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A read on a real PCM finishes with what the PCM gave and records a reason for every PID it did not, the case summary and report show those reasons and the read's notes, stored and permanent codes get separate rows, a case never mixes simulated and real OBD reads, and `ATDPN` goes through the line cleaner.

**Architecture:** The protocol layer gets `ecu_reply` (one ECU's answer to a PID request, a negative response returned instead of raised) and `BUSY_NRC`. A busy reply from the PCM raises `ObdBusy`, a subclass of `ObdAdapterError` marked transient, so the existing retry path in `ObdReader._request` retries it once under the same cap. A new `ObdReader._pid` helper returns `(data, reason)` and is used for `0101`, the support maps, freeze-frame PIDs and live PIDs. `LiveValue` gains `reason`; the event data gains `unavailable`. `Case.obd_kinds()` and `Controller.read_obd` enforce one kind per case. `summary.py` and `render.py` show notes and unavailable PIDs and key code rows by status.

**Tech Stack:** Python 3, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-15-pid-outcomes-design.md`

## Global Constraints

- Reasons are exactly these strings: `not_in_map`, `refused`, `busy`, `no_answer`. An empty reason means the value was read.
- Busy NRCs are exactly `0x21`, `0x22`, `0x78`. Any other negative response from the PCM on a PID request is `refused`.
- Only the PCM's (primary ECU's) reply counts for a mode 01/02 PID request. Other ECUs' replies on such a request, negative or odd, are ignored.
- A busy reply is retried once, through `_request`, under the existing per-read cap `MAX_RETRIES = 3` shared with adapter errors. Once spent, further busy PIDs are marked `busy` without a retry.
- Modes 03/07/0A behave exactly as before: a busy ECU there still fails the read (`test_busy_pcm_on_mode_03_fails_the_read_rather_than_marking_it_unsupported` must keep passing), and "not supported" is still listed per ECU in `unsupported_modes`.
- A negative response or missing PCM data on the first `0100` is fatal (`ObdProtocolError`), never a `KeyError`.
- If a support map (`0120`/`0140`) cannot be read, the PIDs in its range and every later range are tried anyway and are never labelled `not_in_map`.
- A case holds real OBD reads or simulated ones, never both. A case that already mixes both keeps rendering as before and refuses reads of either kind.
- Events and case files recorded before this slice (no `unavailable`, no `adapter`, no `freeze_frame["unavailable"]`) must summarise and render exactly as before.
- The headerless parser (`messages()`, `response_data()`) is not touched.
- Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Whole-suite test command: `python -m pytest -q`.

## Review Focus

- A transmission controller's busy reply on a mode-01 PID must not fail or slow the read (Task 3).
- The retry cap must hold when many PIDs are busy (Task 3).
- `0101`, a support map, and the first `0100` each failing must give a note or a clear error, never a crash (Task 3).
- A mode 03 busy reply must still fail the read (Task 3, existing test).
- An `ATDPN` reply with an echo or blank line, and an empty one (Task 4).
- A case file that already mixes simulated and real reads must refuse both kinds and still render (Tasks 5 and 6).
- Events recorded before this slice must render unchanged (Task 6).
- A code that moves from stored to another status shows a demoted row and a new row rather than silently merging (Task 6).

---

### Task 1: Protocol helpers, `ObdBusy` and reason text

**Files:**
- Modify: `crank/obd/errors.py`
- Modify: `crank/obd/protocol.py`
- Create: `crank/obd/reasons.py`
- Test: `tests/test_obd_protocol.py`, `tests/test_obd_reasons.py` (create)

**Interfaces:**
- Consumes: `ecu_messages`, `_NRC_TEXT`, `ObdProtocolError` (existing).
- Produces:
  - `errors.ObdBusy(nrc: int)`: subclass of `ObdAdapterError`, `.nrc`, `.kind == "busy (NRC 22)"`, `.transient is True`, `.reset is False`.
  - `protocol.BUSY_NRC = (0x21, 0x22, 0x78)`.
  - `protocol.ecu_reply(resp, mode, pid=None, frame=False, *, ecu) -> tuple[bytes | None, int | None] | None`: `(data, None)` for a positive reply from `ecu`, `(None, nrc)` for a negative one, `None` for `NO DATA` or no message from `ecu`. Raises `ObdProtocolError` for an unexpected reply from `ecu` itself; ignores other ECUs entirely.
  - `reasons.REASON_TEXT: dict[str, str]`, `reasons.describe_unavailable(items) -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_obd_protocol.py` (it already imports `pytest`, `P` and `ObdAdapterError`, `ObdProtocolError`):

```python
def test_ecu_reply_positive_and_negative_per_ecu():
    resp = "7E8 03 7F 01 22\r7E9 06 41 00 BE 3F A8 13"
    assert P.ecu_reply(resp, 1, 0, ecu="7E8") == (None, 0x22)
    assert P.ecu_reply(resp, 1, 0, ecu="7E9") == (bytes.fromhex("BE3FA813"), None)
    assert P.ecu_reply(resp, 1, 0, ecu="7EA") is None


def test_ecu_reply_no_data_is_none():
    assert P.ecu_reply("NO DATA", 1, 0x0C, ecu="7E8") is None


def test_ecu_reply_ignores_odd_replies_from_other_ecus():
    resp = "7E8 04 41 0C 0B B8\r7E9 03 41 0D 05"        # 7E9 answers the wrong PID
    assert P.ecu_reply(resp, 1, 0x0C, ecu="7E8") == (bytes([0x0B, 0xB8]), None)


def test_ecu_reply_raises_on_an_unexpected_reply_from_the_wanted_ecu():
    with pytest.raises(ObdProtocolError):
        P.ecu_reply("7E8 03 41 0D 05", 1, 0x0C, ecu="7E8")


def test_busy_nrcs():
    assert P.BUSY_NRC == (0x21, 0x22, 0x78)
```

Create `tests/test_obd_reasons.py`:

```python
from crank.obd.errors import ObdAdapterError, ObdBusy
from crank.obd.reasons import REASON_TEXT, describe_unavailable


def test_obd_busy_is_a_transient_adapter_error_carrying_the_nrc():
    e = ObdBusy(0x22)
    assert isinstance(e, ObdAdapterError) and e.transient and not e.reset
    assert e.nrc == 0x22 and e.kind == "busy (NRC 22)"


def test_reason_text_covers_every_reason():
    assert set(REASON_TEXT) == {"not_in_map", "refused", "busy", "no_answer"}


def test_describe_unavailable_groups_by_reason_worst_first():
    items = [{"name": "D", "reason": "not_in_map"}, {"name": "A", "reason": "refused"},
             {"name": "C", "reason": "no_answer"}, {"name": "B", "reason": "refused"},
             {"name": "E", "reason": "busy"}]
    assert describe_unavailable(items) == ("refused by the PCM: A, B; PCM busy: E; no answer: C; "
                                           "not in the PCM's support map: D")


def test_describe_unavailable_is_empty_for_nothing():
    assert describe_unavailable([]) == ""
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_obd_protocol.py tests/test_obd_reasons.py -q`
Expected: FAIL: `AttributeError: module 'crank.obd.protocol' has no attribute 'ecu_reply'` and `ImportError` for `ObdBusy` / `crank.obd.reasons`.

- [ ] **Step 3: Write the implementation**

In `crank/obd/errors.py`, directly after `ObdAdapterError`:

```python
class ObdBusy(ObdAdapterError):
    """The PCM answered a PID request 'busy', 'conditions not correct' or 'response pending'. Transient,
    so the reader retries it once like a transient adapter error."""

    def __init__(self, nrc: int):
        super().__init__(f"The PCM was busy (negative response {nrc:02X}).",
                         kind=f"busy (NRC {nrc:02X})", transient=True)
        self.nrc = nrc
```

In `crank/obd/protocol.py`, after `_NRC_TEXT`:

```python
BUSY_NRC = (0x21, 0x22, 0x78)
```

and after `response_data_by_ecu`:

```python
def ecu_reply(resp: str, mode: int, pid: int | None = None, frame: bool = False, *, ecu: str):
    """One ECU's answer to a PID request: (data, None) if positive, (None, nrc) if negative, None for
    NO DATA or when `ecu` sent nothing. Other ECUs' replies are ignored. A negative response is returned,
    not raised: the reader decides what it means."""
    msgs = ecu_messages(resp)
    m = msgs.get(ecu) if msgs else None
    if m is None:
        return None
    head = bytes([mode + 0x40] + ([pid] if pid is not None else []) + ([0] if frame else []))
    if m.startswith(head):
        return m[len(head):], None
    if len(m) >= 3 and m[0] == 0x7F and m[1] == mode:
        return None, m[2]
    raise ObdProtocolError(f"Unexpected reply from {ecu} to mode {mode:02X}: {m.hex().upper()}.")
```

Create `crank/obd/reasons.py`:

```python
"""Why a PID has no value. The reader records one of these strings; reports print the words."""
from __future__ import annotations

REASON_TEXT = {
    "not_in_map": "not in the PCM's support map",
    "refused": "refused by the PCM",
    "busy": "PCM busy",
    "no_answer": "no answer",
}
_ORDER = ("refused", "busy", "no_answer", "not_in_map")      # the worrying ones first


def describe_unavailable(items) -> str:
    """'refused by the PCM: A, B; no answer: C' for [{"name", "reason"}, ...]."""
    groups = []
    for reason in _ORDER:
        names = [i.get("name", "?") for i in items if i.get("reason") == reason]
        if names:
            groups.append(f"{REASON_TEXT[reason]}: {', '.join(names)}")
    return "; ".join(groups)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_obd_protocol.py tests/test_obd_reasons.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add crank/obd/errors.py crank/obd/protocol.py crank/obd/reasons.py tests/test_obd_protocol.py tests/test_obd_reasons.py
git commit -m "Add per-ECU PID reply, ObdBusy and reason text

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Simulator negative responses and scenarios

**Files:**
- Modify: `crank/obd/transport.py`
- Test: `tests/test_obd_transport.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `Scenario.pid_nrc: dict` (request → `{"nrc": int, "times": int}`; `times` defaults to 1, 99 = always) and `Scenario.no_answer_pids: tuple` (requests that draw `NO DATA`).
  - `other_ecus[ecu]["pid_nrc"]` with the same shape: that ECU answers a mode 01/02 request with a negative response.
  - Scenarios `pid_refused` (`010D`, NRC `11`, always), `pid_busy_once` (`010C`, NRC `22`, once), `pid_busy_always` (`010C`, NRC `22`, always), `pid_no_answer` (`010D` draws NO DATA), `status_refused` (`0101`, NRC `11`, always), `map_refused` (`0120`, NRC `11`, always), `pcm_refuses_0100` (`0100`, NRC `11`, always), `trans_busy_on_pid` (7E9 busy on `010C`, always), `many_pids_busy` (`010C`, `0105`, `010D`, `0104`, `0106` busy, NRC `22`, always). All use `values=LIVE_RUNNING`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_obd_transport.py`:

```python
def _hdr(name):
    t = SimTransport(name)
    t.open()
    t.send("ATZ")
    t.send("ATH1")
    return t


def test_a_refused_pid_draws_a_negative_response_from_the_pcm():
    t = _hdr("pid_refused")
    t.send("0100")
    assert t.send("010D") == "7E8" + "03" + "7F" + "01" + "11" + "00" * 4
    assert t.send("010C").startswith("7E80441")                # other PIDs still answer normally


def test_a_busy_once_pid_is_busy_the_first_time_only():
    t = _hdr("pid_busy_once")
    t.send("0100")
    assert t.send("010C").startswith("7E8037F0122")
    assert t.send("010C").startswith("7E80441")                # the retry gets the real value


def test_a_no_answer_pid_draws_no_data_even_though_it_is_listed():
    t = _hdr("pid_no_answer")
    t.send("0100")
    assert t.send("010D") == "NO DATA"


def test_another_ecu_can_answer_busy_to_a_pid_while_the_pcm_answers():
    t = _hdr("trans_busy_on_pid")
    t.send("0100")
    lines = t.send("010C").split("\r")
    assert any(l.startswith("7E8") and "7F" not in l for l in lines)
    assert any(l.startswith("7E9037F0122") for l in lines)


def test_the_new_scenarios_exist():
    for n in ("pid_refused", "pid_busy_once", "pid_busy_always", "pid_no_answer", "status_refused",
              "map_refused", "pcm_refuses_0100", "trans_busy_on_pid", "many_pids_busy"):
        assert n in SCENARIOS
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_obd_transport.py -q`
Expected: FAIL: `ValueError: unknown OBD scenario 'pid_refused'`.

- [ ] **Step 3: Write the implementation**

In `crank/obd/transport.py`:

Add to `Scenario` after `adapter`:

```python
    pid_nrc: dict = field(default_factory=dict)         # request -> {"nrc", "times"}: the PCM answers 7F <mode> <nrc>
    no_answer_pids: tuple = ()                          # requests that draw NO DATA though the map lists them
```

Add scenarios to `SCENARIOS` after `flaky_adapter_id`:

```python
    "pid_refused": Scenario(values=LIVE_RUNNING, pid_nrc={"010D": {"nrc": 0x11, "times": 99}}),
    "pid_busy_once": Scenario(values=LIVE_RUNNING, pid_nrc={"010C": {"nrc": 0x22, "times": 1}}),
    "pid_busy_always": Scenario(values=LIVE_RUNNING, pid_nrc={"010C": {"nrc": 0x22, "times": 99}}),
    "pid_no_answer": Scenario(values=LIVE_RUNNING, no_answer_pids=("010D",)),
    "status_refused": Scenario(values=LIVE_RUNNING, pid_nrc={"0101": {"nrc": 0x11, "times": 99}}),
    "map_refused": Scenario(values=LIVE_RUNNING, pid_nrc={"0120": {"nrc": 0x11, "times": 99}}),
    "pcm_refuses_0100": Scenario(values=LIVE_RUNNING, pid_nrc={"0100": {"nrc": 0x11, "times": 99}}),
    "trans_busy_on_pid": Scenario(values=LIVE_RUNNING, other_ecus={
        "7E9": {"pid_nrc": {"010C": {"nrc": 0x22, "times": 99}}}}),
    "many_pids_busy": Scenario(values=LIVE_RUNNING, pid_nrc={
        c: {"nrc": 0x22, "times": 99} for c in ("010C", "0105", "010D", "0104", "0106")}),
```

In `SimTransport.__init__`, after `self.searched, self.errors_left = ...`:

```python
        self.nrc_left: dict = {}
```

Add the method before `_obd`:

```python
    def _nrc_for(self, owner: str, spec: dict, cmd: str):
        """The negative-response code this ECU owes for `cmd` right now, or None. Counts down `times`."""
        s = spec.get(cmd)
        if not s:
            return None
        left = self.nrc_left.setdefault((owner, cmd), s.get("times", 1))
        if left <= 0:
            return None
        self.nrc_left[(owner, cmd)] = left - 1
        return s["nrc"]
```

In `_obd`, replace

```python
        body = self._pcm_body(mode, pid)
        if body == "?":
            return "?"
        items = [(sc.pcm_header, body)] if body is not None else []
```

with

```python
        body = self._pcm_body(mode, pid)
        if body == "?":
            return "?"
        if cmd in sc.no_answer_pids:
            body = None
        nrc = self._nrc_for(sc.pcm_header, sc.pid_nrc, cmd)
        if nrc is not None:
            body = bytes([0x7F, mode, nrc])
        items = [(sc.pcm_header, body)] if body is not None else []
        if mode in (0x01, 0x02):
            for ecu, o in sc.other_ecus.items():
                n = self._nrc_for(ecu, o.get("pid_nrc", {}), cmd)
                if n is not None:
                    items.append((ecu, bytes([0x7F, mode, n])))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_obd_transport.py -q`
Expected: PASS.

Then the whole suite: `python -m pytest -q`
Expected: PASS (nothing reads the new scenarios yet; the allowlist drift test covers them and passes).

- [ ] **Step 5: Commit**

```bash
git add crank/obd/transport.py tests/test_obd_transport.py
git commit -m "Simulate PCM negative responses and missing answers

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Reader PID outcomes

**Files:**
- Modify: `crank/obd/reader.py`
- Test: `tests/test_obd_reader.py`

**Interfaces:**
- Consumes: `P.ecu_reply`, `P.BUSY_NRC`, `ObdBusy` (Task 1); `REASON_TEXT` (Task 1); the Task 2 scenarios.
- Produces:
  - `LiveValue.reason: str = ""` (fifth field, after `value`).
  - `ObdReader._pid(mode, pid=None, frame=False) -> (bytes | None, str)`; the reason is `""` when data came back.
  - `ObdReader.unknown_from: int | None`; `_unknown(pid) -> bool`.
  - The retry note for a busy reply is `Retried <cmd>: the PCM reported busy (NRC 22).`
  - `freeze_frame["unavailable"]` (a list of `{"name", "reason"}`) present only when non-empty.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_obd_reader.py`:

```python
def test_a_refused_pid_is_marked_and_the_read_completes():
    r = read("pid_refused")
    v = live(r)
    assert v["Vehicle speed"].reason == "refused" and v["Vehicle speed"].value is None
    assert v["Engine RPM"].value == 750.0 and v["Engine RPM"].reason == "" and r.notes == []


def test_busy_once_is_retried_noted_and_then_read():
    r = read("pid_busy_once")
    assert live(r)["Engine RPM"].value == 750.0 and live(r)["Engine RPM"].reason == ""
    assert r.notes == ["Retried 010C: the PCM reported busy (NRC 22)."]


def test_busy_always_ends_busy_after_one_retry():
    r = read("pid_busy_always")
    v = live(r)["Engine RPM"]
    assert v.reason == "busy" and v.value is None
    assert r.notes == ["Retried 010C: the PCM reported busy (NRC 22)."]
    assert live(r)["Coolant temperature"].value == 90


def test_the_retry_cap_holds_across_many_busy_pids():
    r = read("many_pids_busy")
    assert len([n for n in r.notes if n.startswith("Retried")]) == 3
    busy = ["Engine RPM", "Coolant temperature", "Vehicle speed", "Engine load", "Short term fuel trim bank 1"]
    assert {live(r)[n].reason for n in busy} == {"busy"}
    assert live(r)["Throttle position"].value is not None


def test_no_data_on_a_listed_pid_is_no_answer_not_unsupported():
    assert live(read("pid_no_answer"))["Vehicle speed"].reason == "no_answer"


def test_pids_the_map_does_not_list_are_not_in_map():
    assert live(read("limited_pcm"))["Control module voltage"].reason == "not_in_map"


def test_a_refused_status_request_leaves_the_light_unknown_and_notes_it():
    r = read("status_refused")
    assert r.mil_on is None and live(r)["Engine RPM"].value == 750.0
    assert any("0101" in n and "refused by the PCM" in n for n in r.notes)


def test_a_refused_support_map_tries_the_pids_in_its_range_anyway():
    r = read("map_refused")
    assert live(r)["Control module voltage"].value == pytest.approx(14.0)      # PID 0x42 is past the map
    assert any("support map 20" in n and "tried anyway" in n for n in r.notes)
    assert all(v.reason != "not_in_map" for v in r.live if v.pid > 0x20)


def test_the_pcm_refusing_the_first_request_fails_with_a_clear_message():
    with pytest.raises(ObdProtocolError, match="refused the supported-PID request.*11"):
        read("pcm_refuses_0100")


def test_another_ecu_busy_on_a_pid_does_not_fail_or_retry_the_read():
    r = read("trans_busy_on_pid")
    assert live(r)["Engine RPM"].value == 750.0 and r.notes == []


def test_freeze_frame_values_that_fail_are_listed_with_a_reason():
    r = read("p2119_zero_rpm")
    miss = {u["name"]: u["reason"] for u in r.freeze_frame["unavailable"]}
    assert miss["Vehicle speed"] == "no_answer" and "Engine RPM" not in miss

    class FreezeBusy(SimTransport):
        def send(self, cmd, timeout=5.0):
            return "7E8 03 7F 02 22" if cmd.strip() == "020500" else super().send(cmd, timeout)

    r = ObdReader(FreezeBusy("p2119_zero_rpm"), retry_pause=0).read()
    miss = {u["name"]: u["reason"] for u in r.freeze_frame["unavailable"]}
    assert miss["Coolant temperature"] == "busy"
    assert {"name": "Engine RPM", "value": 0.0, "unit": "rpm"} in r.freeze_frame["values"]


def test_a_busy_freeze_frame_request_is_noted_not_fatal():
    class FreezeBusy(SimTransport):
        def send(self, cmd, timeout=5.0):
            return "7E8 03 7F 02 22" if cmd.strip() == "020200" else super().send(cmd, timeout)

    r = ObdReader(FreezeBusy("p2119_zero_rpm"), retry_pause=0).read()
    assert r.freeze_frame is None and any("Freeze frame unavailable" in n for n in r.notes)
    assert [c["code"] for c in r.codes] == ["P2119"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_obd_reader.py -q`
Expected: FAIL: `AttributeError: 'LiveValue' object has no attribute 'reason'`, a `ObdProtocolError` "try again" from the busy scenarios, and a `KeyError` from `pcm_refuses_0100`.

- [ ] **Step 3: Write the implementation**

In `crank/obd/reader.py`:

Imports: replace the `.errors` and add `.reasons` lines:

```python
from .errors import ObdAdapterError, ObdBusy, ObdProtocolError, ObdTimeout
from .pids import FREEZE_PIDS, LIVE_PIDS, PIDS
from .reasons import REASON_TEXT
```

(`ObdNotSupported` is no longer needed once `_pcm_data` is removed below; if `grep -n ObdNotSupported crank/obd/reader.py` still shows a use after the edits, keep the import.)

`LiveValue`: add a field after `value`:

```python
    value: float | None          # None = no value (see `reason`)
    reason: str = ""             # "" = read; else not_in_map | refused | busy | no_answer
```

`ObdReader.__init__`: add `self.unknown_from: int | None = None` after `self._retries = 0`.

In `_request`, change the `except` line and the transient note:

```python
            except ObdAdapterError as e:                  # includes ObdBusy: the PCM said busy
                if retried or self._retries >= MAX_RETRIES or not (e.transient or e.reset):
                    raise
                retried = True
                self._retries += 1
                if e.reset:
                    ...                                  # unchanged
                else:
                    who = "the PCM" if isinstance(e, ObdBusy) else "the adapter"
                    self.notes.append(f"Retried {cmd}: {who} reported {e.kind}.")
                    time.sleep(self.retry_pause)
```

Add after `_by_ecu`, and delete `_pcm_data` (`grep -rn "_pcm_data" crank tests` first; it must have no other users):

```python
    def _pid(self, mode: int, pid: int | None = None, frame: bool = False):
        """The PCM's data for one PID request and why not: (data, "") or (None, reason). A busy reply is
        retried once by _request; the PCM refusing a PID never fails the read. Other ECUs are ignored."""
        def parse(resp):
            r = P.ecu_reply(resp, mode, pid, frame, ecu=self.primary)
            if r is not None and r[1] in P.BUSY_NRC:
                raise ObdBusy(r[1])
            return r
        try:
            _, r = self._request(P.request(mode, pid, frame), parse)
        except ObdBusy:
            return None, "busy"
        if r is None:
            return None, "no_answer"
        data, nrc = r
        return (None, "refused") if nrc is not None else (data, "")

    def _unknown(self, pid: int) -> bool:
        """True when the support map covering this PID could not be read."""
        return self.unknown_from is not None and (pid - 1) // 0x20 * 0x20 >= self.unknown_from
```

In `read()`: after `self.notes, self._retries = out.notes, 0` add `self.unknown_from = None`. Replace the block from `data, _ = P.response_data_by_ecu(first_raw, 0x01, 0x00)` through the `for base in (0x20, 0x40):` loop with:

```python
            reply = P.ecu_reply(first_raw, 0x01, 0x00, ecu=self.primary)
            if reply is None or reply[0] is None:
                raise ObdProtocolError(
                    f"The PCM refused the supported-PID request (negative response {reply[1]:02X})."
                    if reply else "The PCM sent no supported-PID map.")
            supported = P.supported_pids(0x00, reply[0])
            for base in (0x20, 0x40):
                if base in supported and self.unknown_from is None:
                    data, why = self._pid(0x01, base)
                    if data:
                        supported |= P.supported_pids(base, data)
                    else:
                        self.unknown_from = base
                        out.notes.append(f"PID support map {base:02X} unavailable "
                                         f"({REASON_TEXT[why or 'no_answer']}); the PIDs it covers were tried anyway.")
```

Replace `_status`, `_freeze` and `_live`:

```python
    def _status(self, out: ObdReadout) -> None:
        data, why = self._pid(0x01, 0x01)
        if not data:
            out.notes.append(f"Check engine light status unavailable: {REASON_TEXT[why or 'no_answer']} on 0101.")
            return
        out.mil_on = bool(data[0] & 0x80)

    def _freeze(self, out: ObdReadout) -> None:
        data, why = self._pid(0x02, 0x02, frame=True)
        if why == "refused":
            out.unsupported_modes.append("02")
            return
        if why == "busy":
            out.notes.append("Freeze frame unavailable: the PCM was busy.")
            return
        if not data or len(data) < 2:
            return
        if data[0] == 0 and data[1] == 0:                  # 42 02 00 00: the PCM holds no freeze frame
            return
        code = P.decode_dtc(data[0], data[1])
        values, unavailable = [], []
        for pid in FREEZE_PIDS:
            d, why = self._pid(0x02, pid, frame=True)
            dec = P.decode_pid(pid, d) if d else None
            if dec:
                values.append({"name": PIDS[pid].name, "value": dec[0], "unit": dec[1]})
            else:
                unavailable.append({"name": PIDS[pid].name, "reason": why or "no_answer"})
        out.freeze_frame = {"code": code, "description": P.describe_dtc(code) or "", "values": values}
        if unavailable:
            out.freeze_frame["unavailable"] = unavailable

    def _live(self, out: ObdReadout, supported: set) -> None:
        for pid in LIVE_PIDS:
            p, value, reason = PIDS[pid], None, ""
            if pid in supported or self._unknown(pid):
                d, reason = self._pid(0x01, pid)
                dec = P.decode_pid(pid, d) if d else None
                if dec:
                    value = dec[0]
                elif not reason:
                    reason = "no_answer"                   # too short to decode
            else:
                reason = "not_in_map"
            out.live.append(LiveValue(pid, p.name, p.unit, value, reason))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_obd_reader.py -q`
Expected: PASS for the new tests. Existing tests may now fail only where they asserted the old text of the live-values line (`Live values read: 2 of 10 (not supported:`); that is fixed in Task 4. If any other existing test fails, read it: update only an assertion that encoded the old behaviour this plan deliberately changes, and record it as a `Ruling:` in the ledger; otherwise it is a bug in the implementation.

Then the whole suite: `python -m pytest -q`
Expected: PASS, apart from the single known assertion in `test_readout_lines_are_plain_language`, which Task 4 updates. `test_busy_pcm_on_mode_03_fails_the_read_rather_than_marking_it_unsupported` must still pass.

- [ ] **Step 5: Commit**

```bash
git add crank/obd/reader.py tests/test_obd_reader.py
git commit -m "Mark PIDs the PCM refuses, cannot answer or is busy on instead of failing the read

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Readout lines, event data and `ATDPN`

**Files:**
- Modify: `crank/obd/reader.py`
- Test: `tests/test_obd_reader.py`

**Interfaces:**
- Consumes: `describe_unavailable`, `REASON_TEXT` (Task 1); `LiveValue.reason` (Task 3); `clean_id` (existing in `adapter.py`).
- Produces:
  - `ObdReadout.lines()`: `Live values read: N of M (<grouped reasons>)`.
  - `ObdReadout.to_event_data()["unavailable"]`: `[{"name", "reason"}]` for every live PID without a value (including `not_in_map`).
  - `out.protocol` comes from `clean_id`; an empty reply raises `ObdProtocolError("The adapter did not report its protocol.")`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_obd_reader.py`, change the assertion in `test_readout_lines_are_plain_language` from

```python
    assert any(l.startswith("Live values read: 2 of 10 (not supported:") for l in lines)
```

to

```python
    assert any(l.startswith("Live values read: 2 of 10 (not in the PCM's support map:") for l in lines)
```

and append:

```python
def test_result_lines_group_the_missing_values_by_reason():
    lines = read("pid_refused").lines()
    assert any(l.startswith("Live values read: 9 of 10 (refused by the PCM: Vehicle speed)") for l in lines)
    assert "(" not in [l for l in read("healthy").lines() if l.startswith("Live values")][0]


def test_event_data_records_every_value_without_a_reason():
    assert read("healthy").to_event_data()["unavailable"] == []
    got = read("pid_refused").to_event_data()["unavailable"]
    assert got == [{"name": "Vehicle speed", "reason": "refused"}]
    assert {"name": "Control module voltage", "reason": "not_in_map"} in read("limited_pcm").to_event_data()["unavailable"]


def test_atdpn_reply_with_an_echo_or_blank_lines_still_parses():
    class Echo(SimTransport):
        def send(self, cmd, timeout=5.0):
            return "ATDPN\r\rA6" if cmd == "ATDPN" else super().send(cmd, timeout)

    assert ObdReader(Echo("healthy"), retry_pause=0).read().protocol == "A6"


def test_an_empty_atdpn_reply_is_a_clear_error():
    class Silent(SimTransport):
        def send(self, cmd, timeout=5.0):
            return "" if cmd == "ATDPN" else super().send(cmd, timeout)

    with pytest.raises(ObdProtocolError, match="did not report its protocol"):
        ObdReader(Silent("healthy"), retry_pause=0).read()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_obd_reader.py -q`
Expected: FAIL: the grouped-line assertions, the missing `unavailable` key, and the `ATDPN` echo (`is_can("ATDPN\rA6")` is false so the read raises "Only CAN vehicles are supported.").

- [ ] **Step 3: Write the implementation**

In `crank/obd/reader.py`:

Import: `from .reasons import REASON_TEXT, describe_unavailable`.

In `ObdReadout.lines()`, replace the `got = ...` / `out.append(f"Live values read: ...` pair with:

```python
        got = [v for v in self.live if v.supported]
        missing = [{"name": v.name, "reason": v.reason or "no_answer"} for v in self.live if not v.supported]
        out.append(f"Live values read: {len(got)} of {len(self.live)}"
                   + (f" ({describe_unavailable(missing)})" if missing else ""))
```

In `to_event_data()`, add before `"adapter"`:

```python
                "unavailable": [{"name": v.name, "reason": v.reason or "no_answer"}
                                for v in self.live if not v.supported],
```

In `read()`, replace `out.protocol = self._ask("ATDPN").strip()` with:

```python
            out.protocol = clean_id(self._ask("ATDPN"), "ATDPN")
            if not out.protocol:
                raise ObdProtocolError("The adapter did not report its protocol.")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_obd_reader.py -q` then `python -m pytest -q`
Expected: PASS, whole suite included.

- [ ] **Step 5: Commit**

```bash
git add crank/obd/reader.py tests/test_obd_reader.py
git commit -m "Group missing live values by reason, record them, and clean the ATDPN reply

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: One kind of OBD read per case

**Files:**
- Modify: `crank/case.py`
- Modify: `crank/controller.py`
- Test: `tests/test_obd_controller.py`

**Interfaces:**
- Consumes: `Transport.sim` (existing).
- Produces:
  - `Case.obd_kinds() -> set[str]`: any of `"real"`, `"simulated"` for the case's OBD `code_read` events.
  - `Controller.read_obd` returns `(None, message)` before opening the adapter when the case already holds the other kind. Messages: `This case already has real OBD reads. Start a new case to use the simulator.` and `This case already has simulated OBD reads. Start a new case to use a real adapter.`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_obd_controller.py` (`_serial_via_sim` is defined earlier in the file):

```python
def test_a_real_read_into_a_simulated_case_is_refused_before_the_adapter_is_touched(tmp_path, monkeypatch):
    c = Controller(SessionStore(tmp_path))
    c.read_obd("healthy")
    fake = _serial_via_sim(monkeypatch)
    r, err = c.read_obd(("serial", "/dev/cu.fake"))
    assert r is None and "already has simulated OBD reads" in err and "real adapter" in err
    assert fake.written == [] and [e.type for e in c.case().events()].count("code_read") == 1


def test_a_simulated_read_into_a_real_case_is_refused(tmp_path, monkeypatch):
    _serial_via_sim(monkeypatch)
    c = Controller(SessionStore(tmp_path))
    assert c.read_obd(("serial", "/dev/cu.fake"))[1] is None
    r, err = c.read_obd("healthy")
    assert r is None and "already has real OBD reads" in err and "simulator" in err
    assert [e.type for e in c.case().events()].count("code_read") == 1


def test_reads_of_the_same_kind_accumulate_and_the_lock_is_free_after_a_refusal(tmp_path, monkeypatch):
    c = Controller(SessionStore(tmp_path))
    assert c.read_obd("healthy")[1] is None
    _serial_via_sim(monkeypatch)
    assert c.read_obd(("serial", "/dev/cu.fake"))[1] is not None          # refused
    assert c.read_obd("p2119_zero_rpm")[1] is None                         # the lock was released
    assert [e.type for e in c.case().events()].count("code_read") == 2


def test_a_case_that_already_mixes_both_kinds_refuses_both(tmp_path, monkeypatch):
    c = Controller(SessionStore(tmp_path))
    c.case().append("code_read", "", {"source": "obd", "sim": True, "codes": []})
    c.case().append("code_read", "", {"source": "obd", "sim": False, "codes": []})
    assert c.case().obd_kinds() == {"real", "simulated"}
    _serial_via_sim(monkeypatch)
    assert c.read_obd("healthy")[0] is None
    assert c.read_obd(("serial", "/dev/cu.fake"))[0] is None
    assert [e.type for e in c.case().events()].count("code_read") == 2


def test_an_empty_case_accepts_either_kind(tmp_path):
    assert Controller(SessionStore(tmp_path)).case().obd_kinds() == set()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_obd_controller.py -q`
Expected: FAIL: `AttributeError: 'Case' object has no attribute 'obd_kinds'` and the mixed reads are recorded.

- [ ] **Step 3: Write the implementation**

In `crank/case.py`, after `append_obd`:

```python
    def obd_kinds(self) -> set[str]:
        """'real' and/or 'simulated' for the OBD reads recorded in this case. A case holds one kind."""
        return {"simulated" if e.data.get("sim") else "real"
                for e in self.events() if e.type == "code_read" and e.data.get("source") == "obd"}
```

In `crank/controller.py`, add before `read_obd`:

```python
    def _wrong_kind(self, transport) -> str:
        """'' if this read may go into the current case, else why not. A case holds real OBD reads or
        simulated ones, never both, so a practice run cannot be mistaken for evidence."""
        kind = "simulated" if transport.sim else "real"
        others = self.case().obd_kinds() - {kind}
        if not others:
            return ""
        have = sorted(others)[0]
        use = "the simulator" if have == "real" else "a real adapter"
        return f"This case already has {have} OBD reads. Start a new case to use {use}."
```

and in `read_obd`, replace `readout = ObdReader(self._obd_transport(source)).read()` with:

```python
                transport = self._obd_transport(source)
                wrong = self._wrong_kind(transport)
                if wrong:
                    return None, wrong
                readout = ObdReader(transport).read()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_obd_controller.py -q` then `python -m pytest -q`
Expected: PASS. If an existing test in another file reads a simulated and a real source into one case, update that test to use two cases and record a `Ruling:` in the ledger.

- [ ] **Step 5: Commit**

```bash
git add crank/case.py crank/controller.py tests/test_obd_controller.py
git commit -m "Keep real and simulated OBD reads out of the same case

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Summary and report: notes, unavailable PIDs, code rows by status

**Files:**
- Modify: `crank/summary.py`
- Modify: `crank/render.py`
- Test: `tests/test_obd_case.py`

**Interfaces:**
- Consumes: `REASON_TEXT` (Task 1); event data `notes` (existing) and `unavailable` (Task 4).
- Produces:
  - `Summary.obd_notes: list[str]` and `Summary.obd_unavailable: list[dict]` from the latest OBD read; `obd_unavailable` leaves out `not_in_map`.
  - `Summary.codes` rows keyed by `(code, ecu, status)`.
  - Markdown and HTML Key data: `Not read from the PCM: Vehicle speed (refused by the PCM)` and `OBD read notes: <notes joined by a space>`, each only when non-empty.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_obd_case.py` (it defines `case`, `read`, `real_read`, `to_markdown`, `to_html`, `build_summary`):

```python
def test_the_latest_reads_notes_and_unavailable_pids_reach_the_summary_and_reports(case):
    r = read("pid_refused")
    case.append_obd(r)
    s = build_summary(case)
    assert s.obd_unavailable == [{"name": "Vehicle speed", "reason": "refused"}] and s.obd_notes == []
    want = "Not read from the PCM: Vehicle speed (refused by the PCM)"
    assert want in to_markdown(s) and want in to_html(s)


def test_retry_notes_are_printed_in_the_reports(case):
    r = read("transient_can_error")
    case.append_obd(r)
    s = build_summary(case)
    assert s.obd_notes == r.notes and r.notes
    assert "OBD read notes: " + " ".join(r.notes) in to_markdown(s)
    assert "OBD read notes: " + " ".join(r.notes) in to_html(s)


def test_a_clean_read_adds_nothing_to_the_reports(case):
    case.append_obd(read("healthy"))
    s = build_summary(case)
    assert s.obd_notes == [] and s.obd_unavailable == []
    assert "OBD read notes" not in to_markdown(s) and "Not read from the PCM" not in to_markdown(s)
    assert "OBD read notes" not in to_html(s) and "Not read from the PCM" not in to_html(s)


def test_values_the_map_does_not_list_are_not_reported_as_missing(case):
    case.append_obd(read("limited_pcm"))
    assert build_summary(case).obd_unavailable == []


def test_the_latest_read_replaces_the_notes_of_an_earlier_one(case):
    case.append_obd(read("transient_can_error"))
    case.append_obd(read("healthy"))
    assert build_summary(case).obd_notes == []


def test_events_without_notes_or_unavailable_keys_still_render(case):
    real_read(case, None)
    s = build_summary(case)
    assert s.obd_notes == [] and s.obd_unavailable == []


def _codes_read(case, rows):
    case.append("code_read", "", {"source": "obd", "sim": False, "ecus": ["7E8"], "codes": [
        {"code": c, "status": st, "ecu": "7E8", "description": ""} for c, st in rows]})


def test_a_code_that_is_stored_and_permanent_shows_as_two_rows(case):
    case.append_obd(read("many_codes"))
    rows = [(c["code"], c["status"]) for c in build_summary(case).codes if c["code"] == "P0335"]
    assert sorted(rows) == [("P0335", "permanent"), ("P0335", "stored")]
    md = to_markdown(build_summary(case))
    assert "P0335 (stored)" in md.replace(" [SIMULATED]", "") and "P0335 (permanent)" in md.replace(" [SIMULATED]", "")


def test_each_status_row_is_demoted_on_its_own(case):
    _codes_read(case, [("P0335", "stored"), ("P0335", "permanent")])
    _codes_read(case, [("P0335", "stored")])                   # the permanent entry is gone
    status = {(c["code"], c["status"]) for c in build_summary(case).codes}
    assert status == {("P0335", "stored"), ("P0335", "not in latest OBD read")}


def test_a_code_changing_status_shows_a_demoted_row_and_the_new_one(case):
    _codes_read(case, [("P0335", "stored")])
    _codes_read(case, [("P0335", "pending")])
    status = sorted(c["status"] for c in build_summary(case).codes)
    assert status == ["not in latest OBD read", "pending"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_obd_case.py -q`
Expected: FAIL: `AttributeError: 'Summary' object has no attribute 'obd_notes'`; the stored-and-permanent test finds one row.

- [ ] **Step 3: Write the implementation**

In `crank/summary.py`, add to `Summary` after `adapter`:

```python
    obd_notes: list = field(default_factory=list)        # notes from the latest OBD read (retries, resets, gaps)
    obd_unavailable: list = field(default_factory=list)  # [{"name","reason"}] it could not read; not_in_map left out
```

In `build_summary`, in the `code_read` loop, change the OBD block and the row key. Replace

```python
            now = {(c["code"], c.get("ecu", "")) for c in rows}
```

with

```python
            now = {(c["code"], c.get("ecu", ""), c.get("status", "")) for c in rows}
            s.obd_notes = [str(n) for n in e.data.get("notes") or []]
            s.obd_unavailable = [u for u in e.data.get("unavailable") or []
                                 if isinstance(u, dict) and u.get("reason") != "not_in_map"]
```

and replace

```python
            codes[(c["code"], c.get("ecu", ""))] = {**c, "sim": sim, "show_ecu": multi}
```

with

```python
            codes[(c["code"], c.get("ecu", ""), c.get("status", ""))] = {**c, "sim": sim, "show_ecu": multi}
```

Also change the type comment on `codes` to `# every (code, ecu, status) ever read, with its latest status`.

In `crank/render.py`, add the import `from .obd.reasons import REASON_TEXT` with the other imports, a helper after `_freeze`:

```python
def _unavailable(items) -> str:
    return ", ".join(f"{u.get('name', '?')} ({REASON_TEXT.get(u.get('reason'), u.get('reason', 'no answer'))})"
                     for u in items)
```

then in `to_markdown`, after the adapter line:

```python
    if s.obd_unavailable:
        out.append("- Not read from the PCM: " + _md(_unavailable(s.obd_unavailable)))
    if s.obd_notes:
        out.append("- OBD read notes: " + _md(" ".join(s.obd_notes)))
```

and in `to_html`, after the adapter entry:

```python
        if s.obd_unavailable:
            data.append("Not read from the PCM: " + e(_unavailable(s.obd_unavailable)))
        if s.obd_notes:
            data.append("OBD read notes: " + e(" ".join(s.obd_notes)))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_obd_case.py -q` then `python -m pytest -q`
Expected: PASS. If an existing summary test fails because a code that changed status used to be one row, update that assertion to the two-row result and record a `Ruling:` in the ledger.

- [ ] **Step 5: Commit**

```bash
git add crank/summary.py crank/render.py tests/test_obd_case.py
git commit -m "Show OBD read notes and unavailable PIDs, and keep stored and permanent rows apart

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 7: README

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: the behaviour from Tasks 1 to 6.
- Produces: documentation only.

- [ ] **Step 1: Find the places to edit**

Run: `grep -n "twenty scenarios\|Adapter identification\|simulator's" README.md`
Expected: the scenario count sentence and the identification paragraph in the OBD section.

- [ ] **Step 2: Edit**

Run `python -c "from crank.obd.transport import SCENARIOS; print(len(SCENARIOS))"` and change the spelled-out count in the sentence "The simulator has twenty scenarios" to that number (29 expected: "twenty-nine").

After the adapter identification paragraph add (matching the surrounding style):

```markdown
**When the PCM does not give a value.** A PID the PCM refuses, answers "busy" to, or does not answer never fails the read: the value is left out and the reason is recorded (not in the PCM's support map, refused by the PCM, PCM busy, no answer). A busy reply is retried once, under the same limit as adapter errors. The result lines group the missing values by reason, and the case summary and report print the latest read's notes (retries, resets, adapter gaps) and any values the PCM would not give. If a PID support map cannot be read, the PIDs it covers are tried anyway. Only a failure on the very first supported-PID request, or a busy reply on a trouble-code request, still fails the read. Trouble codes now show separate rows when a code is both stored and permanent. A case holds real OBD reads or simulated ones, never both: reading from the other kind is refused with a message, so practice runs cannot be mistaken for evidence. The simulator's `pid_refused`, `pid_busy_once`, `pid_busy_always`, `pid_no_answer`, `status_refused`, `map_refused`, `pcm_refuses_0100`, `trans_busy_on_pid` and `many_pids_busy` scenarios exercise these. None of this is checked against a real PCM yet (#3).
```

- [ ] **Step 3: Run the whole suite**

Run: `python -m pytest -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "Document PID outcomes and the one-kind-per-case rule

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```
