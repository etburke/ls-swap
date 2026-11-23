# OBD PIDs, Multi-PID Polling and Watch Charts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Poll watches and reads collect every useful supported mode 01 PID (multi-PID requests, single-PID fallback), and a new Watches page charts any of them, with the linked capture's crank RPM overlaid.

**Architecture:** The PID table grows; protocol gains a multi-PID request builder and reply splitter; the poll watch sends groups of up to 6 PIDs led by `0C` and records one row per PID; a pure series module turns saved rows into per-PID series; a plotly figure builder draws one subplot per unit; the Watches page and the Case page's finished-watch view both use it; MCP `get_watch` takes a `pid`.

**Tech Stack:** Python 3.11, Streamlit, plotly (already a dependency), pytest.

**Spec:** `docs/superpowers/specs/2026-11-23-obd-pids-and-watch-charts-design.md`

## Global Constraints

- Read-only: the allowlist accepts `01` followed by 1 to 6 two-hex-digit PIDs; nothing else is added.
- CAN 11-bit only; PCM replies parsed by header via existing `protocol.ecu_messages` (ISO-TP reassembly).
- Saved poll rows: one row per PID value, `cmd` = `"01%02X" % pid`, plus `req` (the request sent). Rows written before this change have no `req` and must still load and chart.
- Poll cycle target stays `POLL_PERIOD = 0.1` s; `0C` is in every multi-PID request.
- No two y-scales on one plot: one subplot per unit, shared x (seconds from the start of recording).
- Only PIDs in `PIDS` are requested; only PIDs the PCM advertises (bitmaps `0100`, `0120`, `0140`, `0160`) are requested by watches.

## Review Focus

- The PCM answers a multi-PID request with the PIDs in a different order, or across several CAN frames: values still land on the right PID (Task 2 test).
- The PCM leaves a requested PID out of a multi-PID reply: that PID's row is `no_answer`, the others are kept (Task 2, Task 5 tests).
- The PCM rejects multi-PID requests (negative response or NO DATA): the watch falls back to single PIDs for the rest of the run, says so in its notes, and RPM keeps coming (Task 5 test).
- An old RPM-only poll watch or a raw-mode watch opens on the Watches page without error (Task 6, Task 8 tests).
- A watch linked to a capture that is missing, or whose CH1 has no usable crank signal, charts without the overlay rather than failing (Task 7 test).

---

### Task 1: PID table and allowlist

**Files:**
- Modify: `crank/obd/pids.py`
- Modify: `crank/obd/allowlist.py:16`
- Test: `tests/test_obd_allowlist.py`, `tests/test_obd_protocol.py`

**Interfaces:**
- Produces: `PIDS` with the added entries below; `LIVE_PIDS` unchanged (display order for the first 10); new `POLL_PIDS: tuple[int, ...]` = every PID in `PIDS` except `0C`, in table order.

Add (SAE J1979 decodes, `nbytes` in brackets):
`0x08` "Short term fuel trim bank 2" % [1] `b0*100/128-100`; `0x09` "Long term fuel trim bank 2" % [1] same;
`0x0E` "Timing advance" "° BTDC" [1] `b0/2-64`; `0x10` "Mass air flow" "g/s" [2] `u16/100`;
`0x1F` "Run time since start" "s" [2] `u16`; `0x33` "Barometric pressure" "kPa" [1] `b0`;
`0x43` "Absolute load" "%" [2] `u16*100/255`; `0x44` "Commanded equivalence ratio" "λ" [2] `u16*2/65536`;
`0x45` "Relative throttle position" "%" [1] `b0*100/255`; `0x46` "Ambient air temperature" "°C" [1] `b0-40`;
`0x47` "Throttle position B" "%" [1] `b0*100/255`; `0x49` "Accelerator pedal position D" "%" [1] `b0*100/255`;
`0x4A` "Accelerator pedal position E" "%" [1] `b0*100/255`; `0x4C` "Commanded throttle actuator" "%" [1] `b0*100/255`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_obd_allowlist.py
@pytest.mark.parametrize("cmd", ["010C", "010C0E", "010C0E11454749", "010C 0E 11"])
def test_mode01_one_to_six_pids_allowed(cmd):
    assert allowlist.check(cmd).startswith("01")

@pytest.mark.parametrize("cmd", ["010C0E1145474949", "010", "010C0", "0904", "22F40C"])
def test_mode01_seven_pids_or_odd_lengths_refused(cmd):
    with pytest.raises(ObdForbiddenCommand):
        allowlist.check(cmd)

# tests/test_obd_protocol.py
@pytest.mark.parametrize("pid,data,value", [(0x0E, b"\x8c", 6.0), (0x4C, b"\x54", 32.94), (0x44, b"\x80\x00", 1.0),
                                            (0x10, b"\x01\xf4", 5.0), (0x46, b"\x30", 8)])
def test_new_pid_decodes(pid, data, value):
    assert P.decode_pid(pid, data)[0] == pytest.approx(value, abs=0.01)

def test_poll_pids_exclude_rpm_and_cover_table():
    assert 0x0C not in POLL_PIDS and set(POLL_PIDS) | {0x0C} == set(PIDS)
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_obd_allowlist.py tests/test_obd_protocol.py -q` — Expected: new tests FAIL.
- [ ] **Step 3: Implement** the table entries, `POLL_PIDS`, and allowlist pattern `01(?:[0-9A-F]{2}){1,6}` replacing `01[0-9A-F]{2}` (update the module docstring's list).
- [ ] **Step 4: Run** the same command — Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "OBD: add throttle, pedal, timing and other standard PIDs; allow 1-6 PIDs per mode 01 request"`

### Task 2: Multi-PID request and reply splitting

**Files:**
- Modify: `crank/obd/protocol.py`
- Test: `tests/test_obd_protocol.py`

**Interfaces:**
- Produces: `request_multi(pids: Sequence[int]) -> str` (e.g. `"010C0E11"`, 1–6 PIDs, ValueError otherwise);
  `split_multi(data: bytes, pids: Sequence[int]) -> dict[int, bytes]` where `data` is the PCM message with the leading `0x41` removed; walks (pid, `PIDS[pid].nbytes` bytes) pairs in whatever order they arrive; stops at the first PID not in `pids` or a truncated value; returns only complete values.

- [ ] **Step 1: Write failing tests**

```python
def test_request_multi_builds_and_limits():
    assert P.request_multi([0x0C, 0x0E, 0x11]) == "010C0E11"
    with pytest.raises(ValueError):
        P.request_multi([0x0C] * 7)

def test_split_multi_any_order_and_missing():
    data = bytes([0x11, 0x54, 0x0C, 0x02, 0x08, 0x0E, 0x8C])      # 11, then 0C, then 0E; 4C left out
    assert P.split_multi(data, [0x0C, 0x0E, 0x11, 0x4C]) == {0x11: b"\x54", 0x0C: b"\x02\x08", 0x0E: b"\x8c"}

def test_split_multi_truncated_value_dropped():
    assert P.split_multi(bytes([0x0C, 0x02]), [0x0C]) == {}

def test_multi_reply_across_isotp_frames():
    resp = "7E810114100C02080E8C\r7E8211154455C470049\r7E822104A114C540000"
    msg = P.ecu_messages(resp)["7E8"]
    got = P.split_multi(msg[1:], [0x0C, 0x0E, 0x11, 0x45, 0x47, 0x49])
    assert set(got) == {0x0C, 0x0E, 0x11, 0x45, 0x47, 0x49}
```
(Build the multi-frame fixture with the simulator's `_isotp` if hand-written bytes drift; the assertion is what matters.)

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_obd_protocol.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement** both functions.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "OBD protocol: build multi-PID requests and split their replies"`

### Task 3: Simulator answers multi-PID requests

**Files:**
- Modify: `crank/obd/transport.py` (`LIVE_RUNNING`, `SCENARIOS`, `SimTransport._obd`, `_pcm_body`)
- Test: `tests/test_obd_transport.py`

**Interfaces:**
- Consumes: Task 1 PIDs.
- Produces: `LIVE_RUNNING` also holds `0x0E, 0x33, 0x45, 0x47, 0x49, 0x4A, 0x4C` (plausible idle bytes); bitmap for `0x40` advertises `0x60` when needed and `0160` answers; mode 01 requests with several PIDs answer `41` + (pid, data) for each supported PID (the crank RPM curve applies to `0C` inside a group too); new scenario `"no_multi_pid"` (values `LIVE_RUNNING`, monitor `"crank_rpm"`) answering any multi-PID request with `7F 01 12`; scenario `"crank_poll_multi"` = `LIVE_RUNNING` with monitor `"crank_rpm"`.

- [ ] **Step 1: Write failing tests**

```python
def test_sim_multi_pid_reply_splits_back():
    t = SimTransport("healthy"); t.open(); t.send("ATH1")
    msg = P.ecu_messages(t.send("010C0E4C"))["7E8"]
    assert msg[0] == 0x41 and set(P.split_multi(msg[1:], [0x0C, 0x0E, 0x4C])) == {0x0C, 0x0E, 0x4C}

def test_sim_no_multi_pid_refuses_groups_but_answers_singles():
    t = SimTransport("no_multi_pid"); t.open(); t.send("ATH1")
    assert P.ecu_messages(t.send("010C0E"))["7E8"][:3] == bytes([0x7F, 0x01, 0x12])
    assert P.ecu_reply(t.send("010C"), 0x01, 0x0C, ecu="7E8")[1] is None
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_obd_transport.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — Expected: PASS, and `tests/test_obd_reader.py` still passes.
- [ ] **Step 5: Commit** `git commit -m "OBD sim: answer multi-PID requests; add no_multi_pid and crank_poll_multi scenarios"`

### Task 4: Read collects every supported known PID

**Files:**
- Modify: `crank/obd/reader.py` (`_live`, `_finish_stopped`)
- Test: `tests/test_obd_reader.py`

**Interfaces:**
- Produces: `read_order(supported: set[int]) -> list[int]` in `crank/obd/pids.py`: `LIVE_PIDS` first, then the remaining `PIDS` in table order; the reader requests `[p for p in read_order(...) if p in supported or unknown]` (single-PID requests, as today). PIDs in `LIVE_PIDS` that are not supported still produce a `not_in_map` row (unchanged); added PIDs that are not supported produce no row.

- [ ] **Step 1: Write failing test**

```python
def test_read_includes_new_supported_pids(sim_read):        # use the file's existing helper for a healthy sim read
    out = sim_read("healthy")
    got = {v.pid: v.value for v in out.live}
    assert got[0x4C] is not None and got[0x0E] is not None
    assert 0x10 not in got                                   # not advertised by the sim: no row
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_obd_reader.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests/test_obd_reader.py tests/test_obd_case.py tests/test_obd_controller.py` — Expected: PASS (fix any count-based assertions that relied on exactly 10 live rows).
- [ ] **Step 5: Commit** `git commit -m "OBD read: request every supported PID in the table"`

### Task 5: Poll watch sends PID groups with single-PID fallback

**Files:**
- Modify: `crank/obd/watch.py` (`_run_poll`, `_poll_rpm` → `_poll_group`, `summarize_poll`, `END_NOTES`)
- Modify: `crank/obd/pids.py` (grouping helper)
- Test: `tests/test_obd_watch.py`

**Interfaces:**
- Consumes: `request_multi`, `split_multi`, `supported_pids`, `POLL_PIDS`, sim scenarios.
- Produces:
  - `poll_groups(supported: set[int]) -> list[tuple[int, ...]]` in `pids.py`: the supported `POLL_PIDS` chunked into fives, each group prefixed with `0x0C`; `[(0x0C,)]` when no others are supported.
  - Poll rows: `{"t", "cmd": "01XX", "req", "raw", "value", "unit", "reason"}`, one per PID in the request.
  - `job.notes` gains `END_NOTES["single_pid"] = "The PCM refused multi-PID requests; polled one PID at a time (RPM every other request)."` when the fallback triggers.
  - `summarize_poll(...)["pids"] = {"0C": {"name", "unit", "answered", "min", "max"}, ...}` for every PID with at least one row; existing `"rpm"` and `"voltage"` keys unchanged.
- Behaviour: at the start of polling, request `0100`/`0120`/`0140`/`0160` singly (stop at the first map that does not flag its successor) to get `supported`; each cycle sends the next group. Fallback triggers when a group reply is a negative response, NO DATA, or carries no `0C`, on the first two attempts; after it, cycles alternate `010C` and the next supported PID in `POLL_PIDS`. A group reply missing a non-RPM PID records that PID with reason `no_answer`. `latest_rpm`/`max_rpm`/`monitoring` keep working off `0C`.

- [ ] **Step 1: Write failing tests**

```python
def test_poll_groups_lead_with_rpm_and_hold_six():
    groups = poll_groups({0x0C, 0x0E, 0x11, 0x45, 0x47, 0x49, 0x4A, 0x4C, 0x42})
    assert all(g[0] == 0x0C and len(g) <= 6 for g in groups)
    assert {p for g in groups for p in g} == {0x0C, 0x0E, 0x11, 0x45, 0x47, 0x49, 0x4A, 0x4C, 0x42}

def test_poll_watch_records_many_pids(run_poll_watch):      # existing helper pattern in this file: run a sim poll watch briefly
    meta, rows = run_poll_watch("crank_poll_multi", seconds=1.5)
    cmds = {r["cmd"] for r in rows}
    assert {"010C", "010E", "014C"} <= cmds
    rpm_reqs = [r for r in rows if r["cmd"] == "010C"]
    assert all("0C" in r["req"][2:] for r in rpm_reqs)
    assert "4C" in meta["summary"]["pids"] and meta["summary"]["rpm"]["answered"] > 0

def test_poll_watch_falls_back_to_single_pids(run_poll_watch):
    meta, rows = run_poll_watch("no_multi_pid", seconds=1.5)
    assert any("one PID at a time" in n for n in meta["notes"])
    later = [r for r in rows if r.get("req") and len(r["req"]) == 4]
    assert later and any(r["cmd"] == "010C" and r["value"] is not None for r in later)
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_obd_watch.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests/test_obd_watch.py tests/test_obd_watch_controller.py tests/test_mcp_tools.py` — Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "OBD watch: poll groups of up to 6 PIDs led by RPM, fall back to single PIDs"`

### Task 6: Series from saved watches

**Files:**
- Create: `crank/obd/series.py`
- Modify: `crank/mcp_tools.py` (`_rpm_series`, `get_watch`)
- Test: `tests/test_obd_series.py`, `tests/test_mcp_tools.py`

**Interfaces:**
- Produces:
  - `watch_pids(meta: dict, rows: list[dict]) -> list[int]`: PIDs with at least one value, `0C` first then table order; raw-mode watches return `[0x0C]` when decoded engine speed exists, else `[]`.
  - `pid_series(meta: dict, rows: list[dict], pid: int) -> Series` where `Series` is a dataclass `(points: list[tuple[float, float]], name: str, unit: str, source: str)`; `points` are (seconds since `meta["started"]`, value). Raw mode + `0C` → decoded engine speed, `source="decoded from raw CAN frames (best-effort)"`; poll → rows with `cmd == "01%02X" % pid`, `source="01%02X (official)"`.
  - MCP `get_watch(watch_id, session=None, max_points=500, pid="0C")`: `pid` is two hex digits; response keeps `engine_speed_series` key **only when pid is 0C** (back-compatible) and always adds `"series"`, `"pid"`, `"unit"`, `"available_pids": ["0C", "0E", ...]`. Unknown or absent pid → ToolError naming the available ones.

- [ ] **Step 1: Write failing tests**

```python
def test_old_rpm_only_poll_watch_still_charts():
    meta = {"mode": "poll", "started": 100.0}
    rows = [{"t": 100.5, "cmd": "010C", "raw": "", "value": 130.0, "unit": "rpm", "reason": ""}]
    assert watch_pids(meta, rows) == [0x0C]
    s = pid_series(meta, rows, 0x0C)
    assert s.points == [(0.5, 130.0)] and s.unit == "rpm" and "official" in s.source

def test_raw_watch_series_is_best_effort():
    meta = {"mode": "raw", "started": 0.0}
    rows = [{"t": 1.0, "decoded": [{"name": "Engine speed", "value": 120.0, "decoder": "x"}]}]
    assert pid_series(meta, rows, 0x0C).source.startswith("decoded")

def test_get_watch_pid_param(tools_with_multi_watch):        # build via a sim crank_poll_multi watch in the test session
    out = tools_with_multi_watch.get_watch("001", pid="4C")
    assert out["unit"] == "%" and out["series"] and "engine_speed_series" not in out
    with pytest.raises(ToolError, match="0C"):
        tools_with_multi_watch.get_watch("001", pid="10")
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_obd_series.py tests/test_mcp_tools.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement**; replace `_rpm_series` callers with `pid_series`; update the `get_watch` docstring (it is the MCP tool description).
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "Watch series for any PID; MCP get_watch takes a pid"`

### Task 7: Watch figure with capture RPM overlay

**Files:**
- Modify: `crank/analysis.py` (`SignalReport`, `analyze_signal`)
- Create: `crank/watch_plot.py`
- Test: `tests/test_watch_plot.py`, `tests/test_analysis.py`

**Interfaces:**
- Produces:
  - `SignalReport.rev_rpm: list[tuple[float, float]]` = (mid-time of each complete revolution between consecutive gaps, 60/period), only revolutions with `th.teeth` edges.
  - `build_watch_figure(meta: dict, rows: list[dict], pids: list[int], capture: CaptureData | None = None, th: Thresholds | None = None) -> plotly.graph_objects.Figure`: one subplot per distinct unit among `pids` (order of first appearance), shared x "Seconds from start of recording"; one line per PID named by `PIDS[pid].name`; on the rpm subplot, when `capture` is given and its CH1 analysis has `rev_rpm`, a marker+line trace "Scope CH1 (per rev)" at `meta["trigger_t"] - meta["started"] + t`; a vertical line "Scope trigger" when `trigger_t` is set. Empty `pids` → figure with a single annotation "Pick a PID to plot".

- [ ] **Step 1: Write failing tests**

```python
def test_rev_rpm_from_sim_capture():
    rep = analyze_capture(generate("healthy")).reports["ch1"]
    assert rep.rev_rpm and all(50 < r < 1000 for _, r in rep.rev_rpm)

def test_figure_one_subplot_per_unit_and_overlay(multi_watch_and_capture):
    meta, rows, cap = multi_watch_and_capture
    fig = build_watch_figure(meta, rows, [0x0C, 0x4C, 0x11, 0x0E], cap)
    names = [tr.name for tr in fig.data]
    assert "Scope CH1 (per rev)" in names and "Commanded throttle actuator" in names
    assert len({tr.yaxis for tr in fig.data}) == 3              # rpm, %, ° BTDC

def test_figure_without_usable_capture_has_no_overlay(multi_watch_and_capture):
    meta, rows, _ = multi_watch_and_capture
    flat = CaptureData(np.linspace(0, 1, 1000), {1: np.zeros(1000)}, {1: "Crank @ PCM"})
    names = [tr.name for tr in build_watch_figure(meta, rows, [0x0C], flat).data]
    assert names == ["Engine RPM"]
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_watch_plot.py tests/test_analysis.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement** with `plotly.subplots.make_subplots(rows=n, shared_xaxes=True)`.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "Watch figure: one subplot per unit, scope per-rev RPM overlay"`

### Task 8: Watches page and Case page view

**Files:**
- Modify: `app.py` (`PAGES`, page dispatch), `case_ui.py` (`_watch_results`)
- Create: `watch_ui.py` with `render_watch(ctrl, session_name: str, watch_id: str, key: str) -> None` and `page_watches(ctrl) -> None`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `watch_pids`, `build_watch_figure`, `Session.list_watches/load_watch/load_capture`.
- Produces: page "Watches" after "Results" in `PAGES`. `page_watches`: selectbox of the current session's watches (newest first, label = id + mode + headline), then `render_watch`. `render_watch`: the case summary line (as `_watch_results` does today), a multiselect of `watch_pids` labelled `"%02X %s (%s)" % (pid, name, unit)` defaulting to `[0x0C]`, the figure via `st.plotly_chart`, the source caption (official vs best-effort), the CAN ID table for raw mode, and any watch notes. The linked capture is loaded when `meta["capture_id"]` exists and loads; otherwise no overlay. `_watch_results` becomes a call to `render_watch` for the finished job.

- [ ] **Step 1: Write failing test**

```python
def test_watches_page_lists_and_charts_a_saved_watch(app_with_sim_watch):   # AppTest with a sim crank_poll_multi watch saved
    at = app_with_sim_watch
    at.radio(key="page").set_value("Watches").run()
    assert not at.exception
    assert any("Engine RPM" in str(o) for o in at.multiselect)
    assert at.get("plotly_chart")
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_app.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite** `.venv/bin/python -m pytest -q` — Expected: all pass.
- [ ] **Step 5: Update README** (OBD watch section: multi-PID polling, fallback, Watches page; allowlist line) and commit `git commit -m "Watches page: chart any saved watch's PIDs with the scope RPM overlay"`
