# Channel Roles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every capture records what each scope channel is connected to, and the analysis, plots, verdicts and MCP tools follow those roles instead of fixed channel numbers.

**Architecture:** A `crank/roles.py` module defines the roles (from the E38 pinout) and the legacy default layout. `ChannelConfig` gains `role` and `num`; captures save roles in `CaptureData.meta["roles"]`; corrections for saved captures live in the capture `.json` and are applied when the capture loads. `analyze_capture` picks channels by role; non-analysed roles get basic stats. The Capture and Results pages and the MCP tools expose roles.

**Tech Stack:** Python 3.11, Streamlit, plotly, pytest.

**Spec:** `docs/superpowers/specs/2026-11-23-channel-roles-design.md`

## Global Constraints

- `.npz` scope data is never rewritten; role corrections go in `captures/NNN.json` under `"roles"`.
- Case entries stay append-only; a correction adds an `observation` entry, it never edits the capture's entry.
- A capture with no saved roles analyses exactly as today (legacy layout: CH1 crank at ECM, CH2 crank at sensor, CH3 crank 5V reference, CH4 crank low reference).
- Report keys stay `ch{n}` (and `ch1_rel` becomes `ecm_rel`, see Task 3) so saved snapshots and MCP consumers keep working.
- Role keys (stable, used in files and MCP): `crank_ecm, crank_sensor, crank_ref, crank_low, cam, cam_ref, cam_low, est, coil_low, injector, ecm_ground, ecm_ign, ecm_batt, tp, tac, app, map, fuel_pump_relay, starter_relay, other`. Numbered roles (`num` 1-8 meaningful): `est, injector, coil_low (1=odd, 2=even), tp, tac, app`.

## Review Focus

- A legacy capture (no roles anywhere) gives byte-for-byte the same findings as before this branch (Task 3 test against the sim scenarios' expected codes).
- A capture with no crank-role channel at all reports "insufficient data" with a message naming roles, not a crash (Task 3 test).
- A capture with two 5V-reference channels (crank and cam) or two ground channels reports the worst one and names its channel (Task 3 test).
- Correcting roles on a capture whose `.json` is missing or has no `roles` key works and survives a reload (Task 2 test).
- A role correction naming a channel the capture does not have is refused with a clear message (Task 2 and Task 6 tests).

---

### Task 1: Roles module and channel config

**Files:**
- Create: `crank/roles.py`
- Modify: `crank/model.py` (`ChannelConfig`, `default_channels`)
- Test: `tests/test_roles.py`, `tests/test_model.py`

**Interfaces:**
- Produces:
  - `Role` dataclass `(key: str, name: str, pins: str, kind: str, numbered: bool)`; `kind` in `"crank", "ref", "low", "basic"`.
  - `ROLES: dict[str, Role]` with the keys in Global Constraints. Names and pin hints from the spec table, e.g. `crank_ecm` "Crank signal at ECM" "J2-68"; `est` "Ignition control (EST)" "IC 1-8: J2-70/73/77/76/75/74/72/71"; `injector` "Injector control (low side)" "inj 1-8: J2-20/37/38/39/18/19/40/17"; `ecm_ground` "ECM ground" "J1-73".
  - `LEGACY_ROLES: dict[int, tuple[str, int | None]] = {1: ("crank_ecm", None), 2: ("crank_sensor", None), 3: ("crank_ref", None), 4: ("crank_low", None)}`.
  - `role_label(key: str, num: int | None) -> str`, e.g. `"Ignition control (EST) 3"`, `"Coil low reference (odd bank)"`.
  - `parse_role(text: str) -> tuple[str, int | None]`: accepts `"cam"`, `"est:3"`; ValueError naming valid keys for an unknown key, or a number outside 1-8, or a number on an unnumbered role.
  - `ChannelConfig` gains `role: str = "other"` and `num: int | None = None`; `default_channels()` sets the legacy roles.

- [ ] **Step 1: Write failing tests**

```python
def test_every_role_has_kind_and_pins():
    assert set(ROLES) >= {"crank_ecm", "crank_sensor", "crank_ref", "crank_low", "cam", "est", "injector", "ecm_ground", "other"}
    assert all(r.kind in ("crank", "ref", "low", "basic") for r in ROLES.values())
    assert ROLES["crank_ecm"].pins == "J2-68" and ROLES["cam"].kind == "basic" and ROLES["cam_ref"].kind == "ref"

def test_parse_role():
    assert parse_role("cam") == ("cam", None) and parse_role("est:3") == ("est", 3)
    for bad in ("nope", "est:9", "cam:2"):
        with pytest.raises(ValueError):
            parse_role(bad)

def test_role_label():
    assert role_label("est", 3) == "Ignition control (EST) 3"
    assert role_label("coil_low", 1) == "Coil low reference (odd bank)"

def test_default_channels_carry_legacy_roles():          # tests/test_model.py
    assert {c.n: (c.role, c.num) for c in default_channels()} == LEGACY_ROLES
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_roles.py tests/test_model.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — Expected: PASS; `tests/test_app.py` still passes (the channel table gains two columns).
- [ ] **Step 5: Commit** `git commit -m "Channel roles: role table from the E38 pinout; channels carry a role"`

### Task 2: Captures save roles; corrections in the capture .json

**Files:**
- Modify: `crank/scope.py` (`RigolScope.fetch`, `SimScope.fetch`), `crank/sim.py` (`generate`), `crank/store.py` (`Session.load_capture`, new `set_capture_roles`, `list_captures`)
- Test: `tests/test_store.py`, `tests/test_scope.py`

**Interfaces:**
- Consumes: Task 1.
- Produces:
  - Captures written from now on carry `meta["roles"] = {"1": {"role": "crank_ecm", "num": None}, ...}` for enabled channels (scope and sim fetch, from `CaptureSettings.channels`; `generate()` uses the legacy roles).
  - `Session.load_capture(cid)` returns data whose `meta["roles"]` is the effective map and `meta["roles_source"]` is `"corrected"` (from the `.json`), `"saved"` (from the `.npz`), or `"legacy"` (neither: `LEGACY_ROLES` for the channels present).
  - `Session.set_capture_roles(cid: str, roles: dict[int, tuple[str, int | None]]) -> None`: validates every channel exists in the capture (ValueError "Capture 002 has no CH5." otherwise) and every role via `parse_role` rules; writes `"roles"` into the capture `.json` (creating the minimal `.json` if missing), merging with roles already effective so unspecified channels keep theirs.
  - `list_captures()` rows gain `"roles_corrected": bool`.

- [ ] **Step 1: Write failing tests**

```python
def test_new_capture_saves_roles(tmp_path):
    data = generate("healthy"); ses = SessionStore(tmp_path).open("s"); cid = ses.add_capture(data)
    got = ses.load_capture(cid)
    assert got.meta["roles"]["2"] == {"role": "crank_sensor", "num": None} and got.meta["roles_source"] == "saved"

def test_legacy_capture_without_roles(tmp_path):
    data = generate("healthy"); data.meta.pop("roles", None)
    ses = SessionStore(tmp_path).open("s"); cid = ses.add_capture(data)
    assert ses.load_capture(cid).meta["roles_source"] == "legacy"
    assert ses.load_capture(cid).meta["roles"]["3"]["role"] == "crank_ref"

def test_correct_roles_survives_reload_and_json_missing(tmp_path):
    ses = SessionStore(tmp_path).open("s"); cid = ses.add_capture(generate("healthy"))
    (ses.path / "captures" / f"{cid}.json").unlink()
    ses.set_capture_roles(cid, {2: ("cam", None), 3: ("est", 1)})
    got = SessionStore(tmp_path).open("s").load_capture(cid)
    assert got.meta["roles_source"] == "corrected"
    assert got.meta["roles"]["2"]["role"] == "cam" and got.meta["roles"]["3"] == {"role": "est", "num": 1}
    assert got.meta["roles"]["1"]["role"] == "crank_ecm"            # untouched channel kept

def test_correct_roles_refuses_missing_channel(tmp_path):
    ses = SessionStore(tmp_path).open("s"); cid = ses.add_capture(generate("healthy"))
    with pytest.raises(ValueError, match="CH5"):
        ses.set_capture_roles(cid, {5: ("cam", None)})

def test_scope_fetch_records_roles():                       # tests/test_scope.py, FakeVisa
    cfg = CaptureSettings(); cfg.channels[1].role = "cam"
    d = RigolScope(inst=FakeVisa()).fetch(cfg)
    assert d.meta["roles"]["2"] == {"role": "cam", "num": None}
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_store.py tests/test_scope.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "Captures save channel roles; corrections live in the capture .json"`

### Task 3: Analysis by role

**Files:**
- Modify: `crank/analysis.py` (`analyze_capture`, `supply_stats`, `correlate_events`, `build_findings`, `Analysis`), `crank/case.py` (`capture_snapshot`)
- Test: `tests/test_analysis.py`

**Interfaces:**
- Consumes: Task 1 `ROLES`, Task 2 `meta["roles"]` (absent → `LEGACY_ROLES`).
- Produces:
  - `roles_of(data: CaptureData) -> dict[int, tuple[str, int | None]]` in `crank/roles.py` (meta roles if present, else legacy for the channels present).
  - `Analysis` gains `roles: dict[int, tuple[str, int|None]]` and `basic: dict[str, dict]` (key `ch{n}` → `{"min", "median", "max", "pulses", "pulse_width_ms_median"}`; pulses counted with a Schmitt trigger at 30%/70% of the channel's 1st-99th percentile span, `None` widths when fewer than 2 pulses).
  - Crank checks run on every `kind == "crank"` channel (report key `ch{n}`). The two-point comparison and `harness_mismatch` use the first `crank_ecm` and first `crank_sensor` channels, only when both exist.
  - Supply: the ref check runs over every `kind == "ref"` channel and reports the worst (`ref_sag_mv`, `ref_p1`, `ref_median`, plus `ref_channel: n`); ground over every `kind == "low"` channel, worst as `gnd_dev_mv`, `gnd_p99`, `gnd_channel`. `correlate_events` uses those two channels. Finding titles name the channel's role label, e.g. "Cam 5V reference sags 410 mV during cranking".
  - The ECM-relative trace is `crank_ecm` minus `crank_low` (first of each), report key `ecm_rel`, label "Crank as the ECM sees it (CHa-CHb)"; absent unless both roles exist.
  - No crank-role channel: one `insufficient_data` finding, title "No crank channels in this capture", detail "Set a channel's role to Crank signal at ECM or Crank signal at sensor."; basic stats still computed.
  - `capture_snapshot` prefers the first `crank_ecm` report, then any crank report.

- [ ] **Step 1: Write failing tests**

```python
@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_legacy_layout_findings_unchanged(scenario):            # no roles in meta => same codes as EXPECTED_CODES
    d = generate(scenario); d.meta.pop("roles", None)
    assert analyze_capture(d).codes >= EXPECTED_CODES[scenario]

def test_cam_est_injector_roles_give_no_bogus_verdicts():
    d = generate("healthy")
    d.meta["roles"] = {"1": {"role": "crank_ecm", "num": None}, "2": {"role": "cam", "num": None},
                       "3": {"role": "est", "num": 1}, "4": {"role": "injector", "num": 1}}
    a = analyze_capture(d)
    assert not a.codes & {"harness_mismatch", "ref_sag", "ground_lift"}
    assert "ecm_rel" not in a.reports and set(a.basic) == {"ch2", "ch3", "ch4"}
    assert a.basic["ch2"]["pulses"] > 0

def test_swapped_probes_follow_roles():
    d = generate("ref_sag")
    d.channels[2], d.channels[3] = d.channels[3], d.channels[2]
    d.meta["roles"] = {"1": {"role": "crank_ecm", "num": None}, "2": {"role": "crank_ref", "num": None},
                       "3": {"role": "crank_sensor", "num": None}, "4": {"role": "crank_low", "num": None}}
    a = analyze_capture(d)
    assert "ref_sag" in a.codes and a.supply["ref_channel"] == 2 and "harness_mismatch" not in a.codes

def test_no_crank_role_is_insufficient_data():
    d = generate("healthy")
    d.meta["roles"] = {str(n): {"role": "other", "num": None} for n in d.channels}
    a = analyze_capture(d)
    assert [f.code for f in a.findings] == ["insufficient_data"] and a.basic

def test_worst_of_two_reference_channels_is_reported():
    d = generate("ref_sag")
    d.channels[2] = np.full_like(d.channels[3], 5.0)
    d.meta["roles"] = {"1": {"role": "crank_ecm", "num": None}, "2": {"role": "cam_ref", "num": None},
                       "3": {"role": "crank_ref", "num": None}, "4": {"role": "crank_low", "num": None}}
    a = analyze_capture(d)
    assert a.supply["ref_channel"] == 3 and "Crank 5V reference" in next(f.title for f in a.findings if f.code == "ref_sag")
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_analysis.py -q` — Expected: new tests FAIL.
- [ ] **Step 3: Implement.** Keep `analyze_signal` unchanged.
- [ ] **Step 4: Run** `tests/test_analysis.py tests/test_case.py tests/test_summary.py tests/test_report.py tests/test_capture.py` — Expected: PASS (update `ch1_rel` references to `ecm_rel`, recorded as a ruling).
- [ ] **Step 5: Commit** `git commit -m "Analysis follows channel roles; basic stats for other roles"`

### Task 4: Results plot by role

**Files:**
- Modify: `crank/report.py` (`build_figure`)
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: `Analysis.roles`, `roles_of`.
- Produces: subplot titles `"CHn · <label> · <role label>"`; the ECM-relative row only when `"ecm_rel"` is in `a.reports` (built from the same two channels); event markers only on crank-role rows.

- [ ] **Step 1: Write failing test**

```python
def test_figure_rows_follow_roles():
    d = generate("healthy")
    d.meta["roles"] = {"1": {"role": "crank_ecm", "num": None}, "2": {"role": "cam", "num": None},
                       "3": {"role": "est", "num": 1}, "4": {"role": "injector", "num": 1}}
    titles = [a.text for a in build_figure(d, analyze_capture(d)).layout.annotations]
    assert not any("ECM sees" in t or "CH1-CH4" in t for t in titles)
    assert any("Cam signal" in t for t in titles) and any("Ignition control (EST) 1" in t for t in titles)
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_report.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "Results plot: rows titled by role; ECM-relative row only with both roles"`

### Task 5: Capture and Results pages

**Files:**
- Modify: `app.py` (`page_capture` channel editor, `page_results`)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: Tasks 1-4.
- Produces:
  - Capture page channel table: `role` column as `st.column_config.SelectboxColumn` over `ROLES` keys shown as `"<name> (<pins>)"`... (Streamlit selectbox columns show raw values: use the role **name** as the stored cell value and map back via a name→key dict), `num` as `NumberColumn(min 1, max 8)`. The wiring hint text above the table is replaced by "Set each channel's Role to what its probe is on."
  - Results page: an expander "Channel roles (source: saved | corrected | legacy layout)" with a `data_editor` of CH, label, role, num and a button `key="save_roles"` "Save roles and re-analyse". Saving calls `Session.set_capture_roles`, re-analyses, and appends an `observation`: `"Capture {cid} re-analysed with corrected roles (CH2: Cam signal; CH3: Ignition control (EST) 1): " + "; ".join(finding titles)`, refs `{"capture": cid}`. A ValueError shows `st.error`.
  - Results page shows `a.basic` as a table "Other channels" when non-empty.

- [ ] **Step 1: Write failing tests**

```python
def test_results_roles_editor_saves_and_logs_a_reanalysis(at):
    _arm_and_finish(at)
    _go(at, "Results")
    ed = at.data_editor(key="roles_editor")                    # the expander's editor
    rows = ed.value.to_dict("records")
    rows[1]["role"] = "Cam signal"; rows[2]["role"] = "Ignition control (EST)"; rows[2]["num"] = 1
    ed.set_value(pd.DataFrame(rows)).run()
    at.button(key="save_roles").click().run()
    assert not at.exception
    texts = [e.text for e in _case().events() if e.type == "observation"]
    assert any("re-analysed with corrected roles" in t and "Cam signal" in t for t in texts)

def test_capture_page_channel_table_has_roles(at):
    _go(at, "Capture")
    assert "role" in at.data_editor(key="chan_editor").value.columns
```

(If `AppTest` cannot drive `data_editor` edits in this Streamlit version, drive the save through a session-state seam the page reads, and record that as a ruling.)

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_app.py -q -k "roles"` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests/test_app.py` — Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "Capture page sets channel roles; Results page corrects them and logs a re-analysis"`

### Task 6: MCP and README

**Files:**
- Modify: `crank/mcp_tools.py` (`analysis_dict`, `list_captures`, new `set_capture_roles`), `crank/mcp_server.py` (register; gated list), `README.md`
- Test: `tests/test_mcp_tools.py`, `tests/test_mcp_server.py`

**Interfaces:**
- Produces:
  - `analysis_dict` adds `"roles": {"1": "crank_ecm", "3": "est:1", ...}`, `"roles_source"`, and `"basic"`.
  - `set_capture_roles(capture_id: str, roles: dict[str, str], session: str | None = None) -> dict`, gated by `_gate()`: keys are channel numbers as strings, values `parse_role` strings; ToolError on bad channel/role; appends the same `observation` as the Results page with `source="mcp"`; returns `{"capture", "roles", "findings": [...]}`. Docstring (the tool description) says it never changes scope data and that the original verdict stays in the case.
  - README: Capture page roles column, Results page correction, the role table summary, and the new MCP tool.

- [ ] **Step 1: Write failing tests**

```python
def test_set_capture_roles_is_gated_and_logs(ctrl, tools):
    cid = make_capture(ctrl, "healthy")
    with pytest.raises(ToolError, match="Allow AI control"):
        tools.set_capture_roles(cid, {"2": "cam"})
    ctrl.ai_control = True
    out = tools.set_capture_roles(cid, {"2": "cam", "3": "est:1"}, session="bench")
    assert out["roles"]["2"] == "cam" and not any(f["code"] == "harness_mismatch" for f in out["findings"])
    assert tools.get_capture_analysis(cid, session="bench")["roles_source"] == "corrected"
    ev = [e for e in ctrl.case().events() if e.type == "observation" and e.source == "mcp"]
    assert ev and "corrected roles" in ev[-1].text
    with pytest.raises(ToolError, match="CH9"):
        tools.set_capture_roles(cid, {"9": "cam"}, session="bench")
    with pytest.raises(ToolError, match="cam"):
        tools.set_capture_roles(cid, {"2": "camshaft"}, session="bench")
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_mcp_tools.py tests/test_mcp_server.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement**; add the tool to the server's registration and to the gated-tool list the server instructions mention.
- [ ] **Step 4: Run the full suite** `.venv/bin/python -m pytest -q` — Expected: all pass.
- [ ] **Step 5: Commit** `git commit -m "MCP: capture roles in analysis; set_capture_roles; README"`
