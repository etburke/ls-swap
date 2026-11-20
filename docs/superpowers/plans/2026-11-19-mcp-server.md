# MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Claude Code (or any MCP client on the same machine) inspect, analyze and, when the owner allows it, drive the crank harness through an MCP endpoint hosted inside the running Streamlit app.

**Architecture:** A new `crank/mcp_tools.py` holds one `Tools` class whose methods are the MCP tools, written over the shared `Controller` and the existing pure modules. A new `crank/mcp_server.py` registers those methods on an `MCPServer` and runs it in a daemon uvicorn thread bound to `127.0.0.1`. Control and write tools are refused unless a plain `Controller.ai_control` flag, set by a sidebar toggle, is on. Case events gain an optional `source` field so AI-authored entries are labeled in the timeline and the report.

**Tech Stack:** Python 3.11, `mcp>=2.3,<3` (`from mcp.server.mcpserver import MCPServer`, `ToolError`, `Client`), uvicorn (already installed with Streamlit), Streamlit 1.65, numpy, pytest.

**Spec:** `docs/superpowers/specs/2026-11-19-mcp-server-design.md`

## Global Constraints

- The endpoint listens on `127.0.0.1` only, path `/mcp`, default port `8765`, overridable with `CRANK_MCP_PORT` (`0` means "pick a free port", used by tests). No token, no LAN access.
- **Allow AI control** is off every time the app starts and is never persisted.
- Gated tools (refuse with `AI control is off...` unless `ctrl.ai_control`): `arm_capture`, `cancel_capture`, `obd_connect`, `obd_disconnect`, `obd_relink`, `obd_read`, `obd_stop`, `obd_cancel`, `add_case_note`, `set_suspect`. Everything else is always available.
- There are no tools that edit or delete case events, delete sessions, change the laptop's mode, scenario, session or thresholds, connect the scope, send raw SCPI or OBD commands, or clear trouble codes.
- AI-armed captures get a label starting `[AI] `. Case events added by tools carry `source: "mcp"`. An event with no source serializes exactly as before (no `source` key).
- Waveforms are never returned whole: `get_capture_signal` returns at most 5000 points per call, using min/max decimation so runt pulses survive.
- Every tool returns JSON-safe data (no numpy types, no NaN/Infinity). Tool failures are raised as the SDK's `ToolError` so the message reaches the client.
- Existing tests, case files and reports must behave as before. Run the whole suite with `.venv/bin/python -m pytest -q`.
- Code style: match the surrounding files (short docstrings, `from __future__ import annotations`, no extra commentary).
- Commit trailer on every commit: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

Inputs the spec implies but its main flow does not exercise, most likely to bite first:

1. **A brand-new, empty session.** The owner connects the assistant before recording anything. Every read tool must return an empty-but-valid result, not an error (Task 4).
2. **A capture containing NaN samples** (the analysis already warns about bad downloads). `get_capture_signal` must still answer and report how many samples were invalid (Task 4).
3. **A very event-heavy capture.** `get_capture_analysis` must cap its event list and say how many were left out, so one call cannot flood the assistant's context (Task 4).
4. **A `session` argument that is not a session** (`"../x"`, `""`, a deleted session). It must be refused with the list of real sessions and never touch the filesystem (Task 3).
5. **The toggle turned off, or a second device acting, mid-run.** Progress stays readable after the toggle goes off, a gated tool says why it refused, and arming while a capture or read is running gives the existing "already running" message (Task 6).

---

### Task 1: Event `source` field

**Files:**
- Modify: `crank/case.py` (`Event`, `Event.to_json`, `Event.from_dict`, `Case.append`, `Case.set_hypothesis`)
- Test: `tests/test_case.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `Event.source: str = ""`; `Case.append(type_, text="", data=None, refs=None, source="") -> Event`; `Case.set_hypothesis(hid, title, status="open", reason="", refs=None, source="") -> Event`. Later tasks pass `source="mcp"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_case.py`:

```python
def test_event_source_defaults_empty_and_is_left_out_of_the_json(case):
    ev = case.append("observation", "plain")
    assert ev.source == ""
    assert "source" not in json.loads(ev.to_json())
    line = (case.path / "events.jsonl").read_text().strip().splitlines()[-1]
    assert "source" not in json.loads(line)


def test_event_source_round_trips(case):
    ev = case.append("observation", "from the assistant", source="mcp")
    assert ev.source == "mcp"
    assert json.loads(ev.to_json())["source"] == "mcp"
    assert case.events()[-1].source == "mcp"


def test_old_logs_without_source_load_unchanged(case):
    with open(case.path / "events.jsonl", "w") as f:
        f.write('{"id":"e0001","ts":"2026-10-11T10:00:00","type":"symptom","text":"old","data":{},"refs":{}}\n')
    assert [(e.text, e.source) for e in case.events()] == [("old", "")]


def test_event_source_must_be_text():
    with pytest.raises(ValueError, match="source"):
        Event.from_dict({"id": "e1", "ts": "t", "type": "symptom", "source": 5})


def test_set_hypothesis_records_the_source(case):
    ev = case.set_hypothesis(None, "Bad PCM", source="mcp")
    assert ev.source == "mcp" and case.hypotheses()["h1"].source == "mcp"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_case.py -q -k "source"`
Expected: FAIL (`TypeError: append() got an unexpected keyword argument 'source'`, `AttributeError: ... 'source'`).

- [ ] **Step 3: Implement**

In `crank/case.py`, replace the `Event` dataclass body from `synth` through `to_json` with:

```python
    synth: bool = False            # synthesized from legacy files; never written to disk
    source: str = ""               # who added it: "" for the owner, "mcp" for an AI assistant

    def to_json(self) -> str:
        d = asdict(self)
        d.pop("synth")
        if not d["source"]:
            d.pop("source")                                  # owner entries and old logs stay byte-identical
        return json.dumps(d, separators=(",", ":"))
```

In `Event.from_dict`, after the `for k in ("data", "refs")` loop and before `return cls(**d)`, add:

```python
        if not isinstance(d.get("source", ""), str):
            raise ValueError("event source must be text")
```

Change `Case.append`'s signature and the `Event(...)` construction:

```python
    def append(self, type_: str, text: str = "", data: dict | None = None,
               refs: dict | None = None, source: str = "") -> Event:
```
```python
            ev = Event(f"e{(max(nums) + 1) if nums else 1:04d}", time.strftime("%Y-%m-%dT%H:%M:%S"),
                       type_, text.strip(), data, refs, source=source)
```

Change `Case.set_hypothesis`'s signature and its `self.append` call:

```python
    def set_hypothesis(self, hid: str | None, title: str, status: str = "open",
                       reason: str = "", refs: dict | None = None, source: str = "") -> Event:
```
```python
            return self.append("hypothesis", "", {"hid": hid, "title": title, "status": status,
                                                  "reason": reason}, refs, source)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_case.py tests/test_obd_case.py -q`
Expected: all PASS (new and existing).

- [ ] **Step 5: Commit**

```bash
git add crank/case.py tests/test_case.py
git commit -m "Record who added a case event with an optional source" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Mark AI-authored entries in the timeline and the report

**Files:**
- Modify: `crank/summary.py` (timeline rows and suspect rows gain `source`), `crank/render.py` (markdown and HTML), `case_ui.py` (`_timeline`)
- Test: `tests/test_render.py`

**Interfaces:**
- Consumes: `Event.source` from Task 1.
- Produces: `Summary.timeline` rows and `Summary.open/ruled_out/confirmed` rows carry a `"source"` key (`""` or `"mcp"`). `render.AI_NOTE` text appears in a report only when at least one row has a source.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_render.py`:

```python
def test_ai_entries_are_marked_in_markdown_and_html(tmp_path):
    case = Case(SessionStore(tmp_path).open("c"))
    case.set_vehicle(Vehicle(make="Toyota"))
    case.append("observation", "Owner saw a spark")
    case.append("observation", "Assistant noted fuel smell", source="mcp")
    case.set_hypothesis(None, "Bad ground strap", source="mcp")
    s = build_summary(case)
    assert [t["source"] for t in s.timeline] == ["", "mcp", "mcp"]
    md, html = to_markdown(s), to_html(s)
    assert "[AI] Noted: Assistant noted fuel smell" in md
    assert "Noted: Owner saw a spark" in md and "[AI] Noted: Owner" not in md
    assert "- [AI] Bad ground strap" in md
    assert "added by an AI assistant" in md and "added by an AI assistant" in html
    assert "[AI]" in html


def test_a_case_with_no_ai_entries_has_no_ai_note(full_case):
    assert "AI assistant" not in to_markdown(build_summary(full_case))
    assert "AI assistant" not in to_html(build_summary(full_case))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_render.py -q -k "ai_"`
Expected: FAIL (`KeyError: 'source'`).

- [ ] **Step 3: Implement**

In `crank/summary.py`, in `build_summary`, change the timeline line to:

```python
    s.timeline = [{"ts": e.ts, "type": e.type, "line": describe(e), "synth": e.synth, "source": e.source}
                  for e in events]
```
and the suspect row to:

```python
        row = {"title": e.data.get("title", ""), "reason": e.data.get("reason", ""),
               "evidence": _evidence(e.refs), "source": e.source}
```

In `crank/render.py`, after `_md`, add:

```python
AI_NOTE = "Entries marked [AI] were added by an AI assistant through the MCP server, not by the owner."


def _ai(row: dict) -> str:
    return "[AI] " if row.get("source") else ""


def _has_ai(s: Summary) -> bool:
    return any(r.get("source") for r in s.timeline + s.ruled_out + s.open + s.confirmed)
```

In `to_markdown`, change these lines (the list items for ruled out, open, confirmed and the timeline) to include the marker, and add the note before the closing disclaimer:

```python
    out += ([f"- {_ai(r)}{_md(r['title'])}: {_md(r['reason'])}" + (f" (evidence: {', '.join(r['evidence'])})" if r["evidence"] else "")
             for r in s.ruled_out] or ["- nothing yet"])
    out += ["", "## Still open"] + ([f"- {_ai(r)}{_md(r['title'])}" for r in s.open] or ["- nothing listed"])
    if s.confirmed:
        out += ["", "## Confirmed"] + [f"- {_ai(r)}{_md(r['title'])}: {_md(r['reason'])}" for r in s.confirmed]
```
```python
    out += ["", "## Timeline"] + ([f"- {t['ts'][:16].replace('T', ' ') or 'undated'}: {_ai(t)}{_md(t['line'])}"
                                  for t in s.timeline] or ["- nothing recorded yet"])
```
```python
    if _has_ai(s):
        out += ["", f"_{AI_NOTE}_"]
    out += ["", "_The verdicts are measurements and rule-based findings from a diagnostic tool, not a diagnosis._"]
```

In `to_html`, make the same three changes (using `e(...)` escaping as the surrounding lines do):

```python
        body.append("<h2>Ruled out</h2>" + (_li(f"{_ai(r)}<b>{e(r['title'])}</b>: {e(r['reason'])}"
                    + (f" <span class='muted'>(evidence: {e(', '.join(r['evidence']))})</span>" if r["evidence"] else "")
                    for r in s.ruled_out) or "<p class='muted'>nothing yet</p>"))
        body.append("<h2>Still open</h2>" + (_li(f"{_ai(r)}{e(r['title'])}" for r in s.open) or "<p class='muted'>nothing listed</p>"))
        if s.confirmed:
            body.append("<h2>Confirmed</h2>" + _li(f"{_ai(r)}{e(r['title'])}: {e(r['reason'])}" for r in s.confirmed))
```
```python
        body.append("<h2>Timeline</h2>" + (_li([f"<span class='muted'>{e(t['ts'][:16].replace('T', ' ') or 'undated')}</span> "
                    f"{_ai(t)}{e(t['line'])}" for t in s.timeline]) or "<p class='muted'>nothing recorded yet</p>"))
        if _has_ai(s):
            body.append(f"<p class='muted'>{e(AI_NOTE)}</p>")
```
(`_ai` returns a fixed string with no user text, so it needs no escaping, except `[` and `]` which are safe in HTML.)

In `case_ui.py` `_timeline`, change the markdown line to:

```python
            ai = "**[AI]** " if e.source else ""
            st.markdown(f"`{e.ts[:16].replace('T', ' ') or 'undated'}` {ai}{describe(e)}")
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest -q`
Expected: the whole suite PASSES (including the two new tests and every existing render, summary and app test).

- [ ] **Step 5: Commit**

```bash
git add crank/summary.py crank/render.py case_ui.py tests/test_render.py
git commit -m "Mark AI-authored entries in the timeline and the report" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Tool layer core, the gate, and the status tools

**Files:**
- Modify: `requirements.txt` (add `mcp>=2.3,<3`), `crank/controller.py` (`ai_control`, `mcp` attributes)
- Create: `crank/mcp_tools.py`, `tests/test_mcp_tools.py`, `tests/conftest.py`

**Interfaces:**
- Consumes: `Controller` (`store`, `session_name`, `mode`, `scenario`, `snapshot()`, `connection_status()`, `obd_job`), `trafficlog.tail_lines`.
- Produces (used by Tasks 4 to 8):
  - `Controller.ai_control: bool = False`, `Controller.mcp = None`.
  - `crank/mcp_tools.py`: constants `AI_LABEL`, `NOTE_TYPES`, `MAX_SIGNAL_POINTS`, `MAX_LOG_LINES`, `EVENT_LIMIT`, `OFF_MESSAGE`; function `jsonable(x)`; function `analysis_dict(analysis, event_limit=EVENT_LIMIT) -> dict`; class `Tools(ctrl)` with helpers `_gate()`, `_session(name|None) -> Session`, `_obd_state() -> dict` and tools `list_sessions`, `get_status`, `get_capture_progress`, `obd_progress`, `get_obd_log`.

- [ ] **Step 1: Install the dependency and add it to `requirements.txt`**

Append the line `mcp>=2.3,<3` to `requirements.txt`, then:

Run: `.venv/bin/pip install -r requirements.txt`
Run: `.venv/bin/python -c "from mcp.server.mcpserver import MCPServer; from mcp.server.mcpserver.exceptions import ToolError; from mcp import Client; print('ok')"`
Expected: `ok`

- [ ] **Step 2: Create `tests/conftest.py`**

Every Streamlit AppTest builds a fresh controller, and each would start a server on port 8765. Make tests use a free port:

```python
import pytest


@pytest.fixture(autouse=True)
def _mcp_free_port(monkeypatch):
    monkeypatch.setenv("CRANK_MCP_PORT", "0")      # any server a test starts picks a free port
```

- [ ] **Step 3: Write the failing tests**

Create `tests/test_mcp_tools.py`:

```python
import json

import numpy as np
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from crank.controller import Controller
from crank.mcp_tools import MAX_LOG_LINES, OFF_MESSAGE, Tools, jsonable
from crank.store import SessionStore


@pytest.fixture
def ctrl(tmp_path):
    return Controller(SessionStore(tmp_path / "sessions"), poll_s=0.0, logs_dir=tmp_path / "logs")


@pytest.fixture
def tools(ctrl):
    return Tools(ctrl)


def make_capture(ctrl, scenario="ref_sag", label="baseline", session="bench"):
    """Run one simulated capture to completion and return its id."""
    ctrl.sync(mode="Simulator", scenario=scenario, session_name=session)
    assert ctrl.arm(label) is None
    ctrl.job.join(30)
    assert ctrl.job.state == "done", ctrl.job.message
    return ctrl.job.saved_id


def test_controller_starts_with_ai_control_off(ctrl):
    assert ctrl.ai_control is False and ctrl.mcp is None


def test_jsonable_converts_numpy_tuples_and_non_finite_numbers():
    out = jsonable({"a": np.float64(1.5), "b": (np.int64(2), np.bool_(True)), "c": np.array([1.0, np.nan]),
                    "d": float("inf"), "e": {"x": {3, 1}}})
    assert out == {"a": 1.5, "b": [2, True], "c": [1.0, None], "d": None, "e": {"x": [1, 3]}}
    json.dumps(out, allow_nan=False)


def test_gate_refuses_with_a_clear_message_until_control_is_on(ctrl, tools):
    with pytest.raises(ToolError, match="AI control is off"):
        tools._gate()
    ctrl.ai_control = True
    tools._gate()
    assert "AI control is off" in OFF_MESSAGE


def test_list_sessions_names_the_current_one(ctrl, tools):
    make_capture(ctrl, session="bench")
    assert tools.list_sessions() == {"current": "bench", "sessions": ["bench"]}


@pytest.mark.parametrize("bad", ["../etc", "", "nope"])
def test_a_session_argument_that_is_not_a_session_is_refused(ctrl, tools, bad):
    make_capture(ctrl, session="bench")
    with pytest.raises(ToolError, match="no session.*bench"):
        tools.get_obd_log(session=bad)
    assert ctrl.store.list_sessions() == ["bench"]          # nothing was created


def test_get_status_is_json_safe_and_describes_everything(ctrl, tools):
    ctrl.sync(mode="Simulator", scenario="healthy", session_name="bench")
    s = tools.get_status()
    json.dumps(s, allow_nan=False)
    assert (s["mode"], s["scenario"], s["session"], s["ai_control"]) == ("Simulator", "healthy", "bench", False)
    assert s["capture"]["state"] == "idle"
    assert s["obd"]["connection"]["state"] == "disconnected" and s["obd"]["read"]["state"] == "idle"


def test_progress_tools_report_idle_and_a_finished_capture(ctrl, tools):
    assert tools.get_capture_progress()["state"] == "idle"
    assert tools.obd_progress() == {"state": "idle", "running": False}
    cid = make_capture(ctrl)
    p = tools.get_capture_progress()
    assert p["state"] == "done" and p["saved_id"] == cid
    assert any(f["severity"] == "fail" for f in p["findings"])


def test_get_obd_log_tails_the_session_log(ctrl, tools):
    ctrl.sync(session_name="bench")
    assert tools.get_obd_log() == {"session": "bench", "lines": [], "note": "No OBD log for this session yet."}
    r, err = ctrl.read_obd("healthy")
    assert err is None
    out = tools.get_obd_log(lines=20)
    assert 0 < len(out["lines"]) <= 20 and any(">>" in l for l in out["lines"])


@pytest.mark.parametrize("n", [0, -1, MAX_LOG_LINES + 1])
def test_get_obd_log_rejects_silly_line_counts(tools, n):
    with pytest.raises(ToolError, match="lines must be between"):
        tools.get_obd_log(lines=n)
```

- [ ] **Step 4: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_tools.py -q`
Expected: FAIL at collection (`ModuleNotFoundError: No module named 'crank.mcp_tools'`).

- [ ] **Step 5: Add the controller attributes**

In `crank/controller.py`, in `Controller.__init__` after `self.obd_job: ObdJob | None = None`, add:

```python
        self.ai_control = False                   # set by the sidebar toggle; gates the MCP control tools
        self.mcp = None                           # the running MCP server handle (crank.mcp_server)
```

- [ ] **Step 6: Create `crank/mcp_tools.py`**

```python
"""The MCP tools: plain methods over the shared Controller. crank.mcp_server registers them.

Read tools always work. Control and write tools call `_gate()` first and refuse unless the owner has
switched on "Allow AI control". Failures are raised as ToolError so the client sees the message."""
from __future__ import annotations

import math
from typing import Any

import numpy as np
from mcp.server.mcpserver.exceptions import ToolError

from .analysis import Analysis
from .obd.trafficlog import tail_lines

AI_LABEL = "[AI] "
NOTE_TYPES = ("observation", "symptom", "change")
MAX_SIGNAL_POINTS = 5000
MAX_LOG_LINES = 500
EVENT_LIMIT = 200
OFF_MESSAGE = ("AI control is off. Ask the owner to switch on 'Allow AI control' in the app's sidebar "
               "to use this tool.")

_REPORT_FIELDS = ("name", "label", "n_edges", "usable", "gaps", "missing", "extra", "runt_hi", "runt_lo",
                  "high_median", "high_lowest", "revs", "revs_good", "rpm_median", "rpm_min", "rpm_max")
_SEVERITY_ORDER = {"fail": 0, "warn": 1, "info": 2}


def jsonable(x):
    """Plain JSON data: numpy values become Python ones, tuples and sets become lists, NaN/inf become None."""
    if isinstance(x, dict):
        return {str(k): jsonable(v) for k, v in x.items()}
    if isinstance(x, (set, frozenset)):
        return [jsonable(v) for v in sorted(x)]
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    if isinstance(x, np.ndarray):
        return jsonable(x.tolist())
    if isinstance(x, np.generic):
        return jsonable(x.item())
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    return x


def analysis_dict(a: Analysis, event_limit: int = EVENT_LIMIT) -> dict:
    """An Analysis as compact JSON: ranked findings, supply numbers, per-signal counts, a capped event list."""
    events = a.events
    return jsonable({
        "findings": [dict(code=f.code, severity=f.severity, title=f.title, detail=f.detail, evidence=f.evidence)
                     for f in sorted(a.findings, key=lambda f: _SEVERITY_ORDER[f.severity])],
        "supply": a.supply,
        "channel_compare": a.compare,
        "signals": {k: {f: getattr(r, f) for f in _REPORT_FIELDS} for k, r in a.reports.items()},
        "events": [{"t": t, "kind": kind} for t, kind in events[:event_limit]],
        "events_total": len(events),
    })


class Tools:
    def __init__(self, ctrl):
        self.ctrl = ctrl

    # -- helpers
    def _gate(self) -> None:
        if not self.ctrl.ai_control:
            raise ToolError(OFF_MESSAGE)

    def _session(self, name: str | None):
        store = self.ctrl.store
        if name is None:
            return store.open(self.ctrl.session_name)
        names = store.list_sessions()
        if name not in names:                       # only names that exist; never build a path from the argument
            raise ToolError(f"There is no session {name!r}. Sessions: {', '.join(names) or 'none'}.")
        return store.open(name)

    def _obd_state(self) -> dict:
        job = self.ctrl.obd_job
        if job is None:
            return {"state": "idle", "running": False}
        out = {"state": job.state, "running": job.running, "seconds": round(job.seconds, 1),
               "session": job.session_name, "message": job.message}
        if job.state in ("done", "stopped"):
            out["stopped"] = bool(getattr(job.readout, "stopped", False))
            out["note"] = "The result was added to the case; call get_case_summary to see it."
        return out

    # -- status (always available)
    def list_sessions(self) -> dict[str, Any]:
        """List the saved sessions (cases) and which one the laptop currently has selected."""
        return {"current": self.ctrl.session_name, "sessions": self.ctrl.store.list_sessions()}

    def get_status(self) -> dict[str, Any]:
        """What the app is doing now: scope mode and scenario, the current session, whether AI control is
        on, the capture state, and the OBD connection and read state."""
        c = self.ctrl
        return jsonable({"mode": c.mode, "scenario": c.scenario, "session": c.session_name,
                         "ai_control": c.ai_control, "capture": c.snapshot(),
                         "obd": {"connection": c.connection_status(), "read": self._obd_state()}})

    def get_capture_progress(self) -> dict[str, Any]:
        """The current capture: idle, armed (waiting for the crank), capturing, analyzing, done (with the new
        capture id and its findings), cancelled or error. Poll this after arm_capture."""
        return jsonable(self.ctrl.snapshot())

    def obd_progress(self) -> dict[str, Any]:
        """The current or last OBD read: state, seconds, and any message. Poll this after obd_read."""
        return jsonable(self._obd_state())

    def get_obd_log(self, lines: int = 100, session: str | None = None) -> dict[str, Any]:
        """The last lines of the adapter traffic log for a session (heartbeats hidden). Diagnostic only."""
        if not 1 <= lines <= MAX_LOG_LINES:
            raise ToolError(f"lines must be between 1 and {MAX_LOG_LINES}.")
        sess = self._session(session)
        path = sess.path / "obd.log"
        if not path.exists():
            return {"session": sess.name, "lines": [], "note": "No OBD log for this session yet."}
        return {"session": sess.name, "lines": tail_lines(path, lines, hide_heartbeats=True)}
```

Note: `_case`, `_load` and the capture tools arrive in Task 4. Do not add them here.

- [ ] **Step 7: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_mcp_tools.py -q`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add requirements.txt crank/controller.py crank/mcp_tools.py tests/test_mcp_tools.py tests/conftest.py
git commit -m "Add the MCP tool layer core: the gate, status and log tools" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Case and capture read tools

**Files:**
- Modify: `crank/mcp_tools.py`
- Test: `tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: Task 3's `Tools`, `jsonable`, `analysis_dict`, `MAX_SIGNAL_POINTS`; `crank.summary.build_summary`; `crank.render.to_markdown/to_html`; `crank.report.decimate`; `crank.analysis.analyze_capture`; `crank.case.Case`.
- Produces: `Tools._case(name|None) -> Case`, `Tools._load(sess, capture_id) -> CaptureData`, `Tools._recorded(case, capture_id) -> list | None`; tools `get_case_summary`, `get_case_report`, `list_captures`, `get_capture_analysis`, `get_capture_signal`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_tools.py`:

```python
from crank.sim import generate


def test_every_read_tool_copes_with_a_brand_new_empty_session(ctrl, tools):
    ctrl.sync(session_name="fresh")
    summary = tools.get_case_summary()
    assert summary["empty"] is True and summary["timeline"] == []
    assert tools.list_captures() == {"session": "fresh", "captures": []}
    assert "Nothing has been recorded" in tools.get_case_report()["text"]
    json.dumps(summary, allow_nan=False)


def test_get_case_summary_returns_the_timeline_and_suspects(ctrl, tools):
    make_capture(ctrl, label="baseline")
    ctrl.case().append("observation", "smells like fuel")
    ctrl.case().set_hypothesis(None, "Bad ground")
    s = tools.get_case_summary()
    assert s["empty"] is False
    assert any("baseline" in t["line"] for t in s["timeline"])
    assert [r["title"] for r in s["open"]] == ["Bad ground"]
    json.dumps(s, allow_nan=False)


def test_get_case_report_formats(ctrl, tools):
    make_capture(ctrl)
    md = tools.get_case_report()
    assert md["format"] == "markdown" and "Diagnostic summary" in md["text"]
    assert "<html" in tools.get_case_report(format="html")["text"]
    with pytest.raises(ToolError, match="markdown.*html"):
        tools.get_case_report(format="pdf")


def test_list_captures_gives_label_time_and_verdict_headline(ctrl, tools):
    make_capture(ctrl, "ref_sag", "baseline")
    caps = tools.list_captures()["captures"]
    assert [(c["id"], c["label"], c["sim"]) for c in caps] == [("001", "baseline", True)]
    assert caps[0]["verdict"].startswith("5V reference sags") and caps[0]["time"]


def test_get_capture_analysis_returns_ranked_findings_and_the_recorded_verdict(ctrl, tools):
    cid = make_capture(ctrl, "ref_sag")
    a = tools.get_capture_analysis(cid)
    json.dumps(a, allow_nan=False)
    assert a["findings"][0]["code"] == "ref_sag" and a["findings"][0]["severity"] == "fail"
    assert a["capture"]["id"] == cid and a["thresholds"]["ref_sag_mv"] == 300.0
    assert a["recorded_verdict"][0]["code"] == "ref_sag"
    assert a["signals"]["ch1"]["n_edges"] > 0 and a["supply"]["ref_sag_mv"] > 300


def test_get_capture_analysis_caps_a_long_event_list(ctrl, tools):
    cid = make_capture(ctrl, "ref_sag")                 # this scenario has hundreds of runt pulses
    a = tools.get_capture_analysis(cid)
    assert len(a["events"]) <= 200 and a["events_total"] >= len(a["events"])
    assert a["events_total"] > 200


def test_an_unknown_capture_lists_the_real_ones(ctrl, tools):
    make_capture(ctrl)
    with pytest.raises(ToolError, match="no capture '009'.*001"):
        tools.get_capture_analysis("009")
    with pytest.raises(ToolError, match="no capture '../x'"):
        tools.get_capture_signal("../x", 1)


def test_get_capture_signal_downsamples_but_keeps_the_extremes(ctrl, tools):
    cid = make_capture(ctrl, "ref_sag")
    data = ctrl.store.open("bench").load_capture(cid)
    sig = tools.get_capture_signal(cid, 3, max_points=500)
    json.dumps(sig, allow_nan=False)
    assert len(sig["t"]) == len(sig["v"]) <= 500 and sig["samples_in_window"] == len(data.t)
    assert min(sig["v"]) == pytest.approx(float(data.channels[3].min()), abs=1e-4)   # the deepest sag survives
    assert sig["label"] == "5V ref @ sensor" and sig["unit"] == "V"


def test_get_capture_signal_window_and_validation(ctrl, tools):
    cid = make_capture(ctrl)
    sig = tools.get_capture_signal(cid, 1, start=0.5, end=0.6, max_points=5000)
    assert sig["t"][0] >= 0.5 and sig["t"][-1] <= 0.6 and sig["returned"] == len(sig["t"])
    with pytest.raises(ToolError, match="Channel 9 was not captured.*1, 2, 3, 4"):
        tools.get_capture_signal(cid, 9)
    with pytest.raises(ToolError, match="max_points must be between 2 and 5000"):
        tools.get_capture_signal(cid, 1, max_points=10_000)
    with pytest.raises(ToolError, match="No samples between 50 and 60 s.*runs from"):
        tools.get_capture_signal(cid, 1, start=50, end=60)


def test_get_capture_signal_survives_invalid_samples_and_counts_them(ctrl, tools):
    d = generate("healthy", seconds=1.0)
    d.channels[1][1_000:30_000] = np.nan                # a stretch of failed samples
    cid = ctrl.store.open("bench").add_capture(d, "damaged")
    ctrl.sync(session_name="bench")
    sig = tools.get_capture_signal(cid, 1, max_points=400)
    json.dumps(sig, allow_nan=False)
    assert sig["nan_samples"] == 29_000
    assert tools.get_capture_analysis(cid)["findings"]    # the analysis still answers (bad_samples warning)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_tools.py -q -k "case or capture or empty"`
Expected: FAIL (`AttributeError: 'Tools' object has no attribute 'get_case_summary'`, and similar).

- [ ] **Step 3: Implement**

In `crank/mcp_tools.py`, extend the imports:

```python
import dataclasses
```
and
```python
from .analysis import Analysis, analyze_capture
from .case import Case
from .render import to_html, to_markdown
from .report import decimate
from .summary import build_summary
```
(replace the existing `from .analysis import Analysis` line).

Add these helpers inside `Tools` after `_session`:

```python
    def _case(self, name: str | None) -> Case:
        return Case(self._session(name))

    @staticmethod
    def _load(sess, capture_id: str):
        ids = [c["id"] for c in sess.list_captures()]
        if capture_id not in ids:                   # only ids that exist; never build a path from the argument
            raise ToolError(f"There is no capture {capture_id!r} in session {sess.name!r}. "
                            f"Captures: {', '.join(ids) or 'none'}.")
        return sess.load_capture(capture_id)

    @staticmethod
    def _recorded(case: Case, capture_id: str):
        """The verdict as it was recorded when the capture finished, or None."""
        for e in case.all_events():
            if e.type == "capture" and e.refs.get("capture") == capture_id and e.data.get("snapshot"):
                return e.data["snapshot"].get("verdict")
        return None
```

Add the tools at the end of the class:

```python
    # -- case and captures (always available)
    def get_case_summary(self, session: str | None = None) -> dict[str, Any]:
        """The case for a session (default: the laptop's current one): vehicle, symptoms, suspects (open,
        ruled out, confirmed), codes, measurements, captures with their recorded verdicts, before/after pairs,
        guided tests not yet tried, and the timeline. Entries added by an AI assistant carry source "mcp".
        Verdicts are what the harness reported, not a diagnosis; simulated data is flagged."""
        s = build_summary(self._case(session))
        return jsonable({**dataclasses.asdict(s), "empty": s.empty})

    def get_case_report(self, format: str = "markdown", session: str | None = None) -> dict[str, Any]:
        """The hand-off report for a session as markdown or html text."""
        if format not in ("markdown", "html"):
            raise ToolError("format must be 'markdown' or 'html'.")
        s = build_summary(self._case(session))
        return {"format": format, "text": to_markdown(s) if format == "markdown" else to_html(s)}

    def list_captures(self, session: str | None = None) -> dict[str, Any]:
        """The captures in a session: id, label, time, whether simulated, and the headline of the verdict
        recorded when each finished."""
        sess = self._session(session)
        case = Case(sess)
        out = []
        for c in sess.list_captures():
            verdict = self._recorded(case, c["id"])
            out.append({"id": c["id"], "label": c.get("label", ""), "time": c.get("time", ""),
                        "sim": bool(c.get("sim")), "note": c.get("note", ""),
                        "verdict": verdict[0]["title"] if verdict else None})
        return {"session": sess.name, "captures": out}

    def get_capture_analysis(self, capture_id: str, session: str | None = None) -> dict[str, Any]:
        """Analyze a saved capture with the laptop's current thresholds: findings ranked worst first with
        their evidence, 5V-reference and ground numbers, per-signal edge and pulse counts, and the problem
        events (capped, with the total). Also returns the verdict recorded when the capture finished. A
        verdict names a likely area and its evidence; it is not a diagnosis, and "insufficient data" is a
        valid answer."""
        sess = self._session(session)
        data = self._load(sess, capture_id)
        meta = next(c for c in sess.list_captures() if c["id"] == capture_id)
        return {"capture": {"id": capture_id, "label": meta.get("label", ""), "time": meta.get("time", ""),
                            "sim": bool(meta.get("sim")), "note": meta.get("note", "")},
                "thresholds": jsonable(dataclasses.asdict(self.ctrl.thresholds)),
                "recorded_verdict": jsonable(self._recorded(Case(sess), capture_id)),
                **analysis_dict(analyze_capture(data, self.ctrl.thresholds))}

    def get_capture_signal(self, capture_id: str, channel: int, start: float | None = None,
                           end: float | None = None, max_points: int = 2000,
                           session: str | None = None) -> dict[str, Any]:
        """A window of one channel's voltage over time (seconds relative to the trigger), downsampled to at
        most max_points (2 to 5000) with a min/max envelope so single-sample runts and dropouts survive.
        Channels: 1 crank at the PCM, 2 crank at the sensor, 3 5V reference, 4 PCM low reference."""
        if not 2 <= max_points <= MAX_SIGNAL_POINTS:
            raise ToolError(f"max_points must be between 2 and {MAX_SIGNAL_POINTS}.")
        sess = self._session(session)
        data = self._load(sess, capture_id)
        if channel not in data.channels:
            have = ", ".join(str(n) for n in sorted(data.channels))
            raise ToolError(f"Channel {channel} was not captured. Captured channels: {have}.")
        t, v = data.t, data.channels[channel]
        lo = t[0] if start is None else start
        hi = t[-1] if end is None else end
        mask = (t >= lo) & (t <= hi)
        if not mask.any():
            raise ToolError(f"No samples between {lo:g} and {hi:g} s. "
                            f"This capture runs from {t[0]:.3f} to {t[-1]:.3f} s.")
        t, v = t[mask], v[mask]
        try:
            td, vd = decimate(t, v, max_points)
        except ValueError:                          # a stretch of all-NaN samples: fall back to plain striding
            step = -(-len(v) // max_points)
            td, vd = t[::step], v[::step]
        return jsonable({"capture": capture_id, "channel": channel, "label": data.labels.get(channel, ""),
                         "unit": "V", "t": np.round(td, 6), "v": np.round(vd, 4),
                         "samples_in_window": len(v), "returned": len(td),
                         "nan_samples": int((~np.isfinite(v)).sum()),
                         "method": "min/max envelope per bin" if len(td) < len(v) else "every sample"})
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_mcp_tools.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add crank/mcp_tools.py tests/test_mcp_tools.py
git commit -m "Add the MCP case and capture read tools" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Compare and what-if analysis tools

**Files:**
- Modify: `crank/mcp_tools.py`
- Test: `tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: Task 4's `_session`, `_load`; `crank.case.capture_snapshot(analysis)`; `crank.model.Thresholds`.
- Produces: `Tools.compare_captures(capture_a, capture_b, session=None)`, `Tools.reanalyze_capture(capture_id, thresholds=None, session=None)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_tools.py`:

```python
def test_compare_captures_shows_what_changed(ctrl, tools):
    a = make_capture(ctrl, "ref_sag", "before")
    b = make_capture(ctrl, "healthy", "after")
    c = tools.compare_captures(a, b)
    json.dumps(c, allow_nan=False)
    assert c["a"]["label"] == "before" and c["b"]["label"] == "after"
    assert "ref_sag" in c["differences"]["only_in_a"] and "signal_clean" in c["differences"]["only_in_b"]
    sag = c["differences"]["numbers"]["ref_sag_mv"]
    assert sag["a"] > 300 and sag["change"] == pytest.approx(sag["b"] - sag["a"]) and sag["change"] < 0


def test_compare_a_capture_with_itself_has_no_differences(ctrl, tools):
    a = make_capture(ctrl)
    d = tools.compare_captures(a, a)["differences"]
    assert d["only_in_a"] == d["only_in_b"] == [] and d["numbers"]["revs"]["change"] == 0


def test_compare_with_an_unknown_capture_is_refused(ctrl, tools):
    a = make_capture(ctrl)
    with pytest.raises(ToolError, match="no capture '009'"):
        tools.compare_captures(a, "009")


def test_reanalyze_applies_thresholds_without_saving_anything(ctrl, tools):
    cid = make_capture(ctrl, "ref_sag")
    sess = ctrl.store.open("bench")
    files_before = sorted(p.name for p in (sess.path / "captures").iterdir())
    events_before = len(ctrl.case().events())
    r = tools.reanalyze_capture(cid, {"ref_sag_mv": 5000})
    assert r["saved"] is False and r["thresholds"]["ref_sag_mv"] == 5000.0
    assert "ref_sag" not in [f["code"] for f in r["findings"]]
    assert ctrl.thresholds.ref_sag_mv == 300.0                       # the laptop's thresholds are untouched
    assert sorted(p.name for p in (sess.path / "captures").iterdir()) == files_before
    assert len(ctrl.case().events()) == events_before


def test_reanalyze_validates_thresholds(ctrl, tools):
    cid = make_capture(ctrl)
    with pytest.raises(ToolError, match="Unknown threshold 'bogus'.*ref_sag_mv"):
        tools.reanalyze_capture(cid, {"bogus": 1})
    with pytest.raises(ToolError, match="must be a finite number"):
        tools.reanalyze_capture(cid, {"hi": "high"})
    with pytest.raises(ToolError, match="must be a finite number"):
        tools.reanalyze_capture(cid, {"hi": float("nan")})
    teeth = tools.reanalyze_capture(cid, {"teeth": 58.0})["thresholds"]["teeth"]
    assert teeth == 58 and type(teeth) is int                        # integer fields stay integers
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_tools.py -q -k "compare or reanalyze"`
Expected: FAIL (`AttributeError: ... 'compare_captures'`).

- [ ] **Step 3: Implement**

In `crank/mcp_tools.py`, change the case import and add the Thresholds import:

```python
from .case import Case, capture_snapshot
from .model import Thresholds
```

Add inside `Tools`, after `_recorded`:

```python
    @staticmethod
    def _overrides(raw: dict) -> dict:
        valid = {f.name: f for f in dataclasses.fields(Thresholds)}
        out = {}
        for key, value in raw.items():
            if key not in valid:
                raise ToolError(f"Unknown threshold {key!r}. Valid thresholds: {', '.join(valid)}.")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ToolError(f"Threshold {key!r} must be a finite number.")
            out[key] = int(value) if valid[key].type in (int, "int") else float(value)
        return out
```

Add the tools at the end of the class:

```python
    def compare_captures(self, capture_a: str, capture_b: str, session: str | None = None) -> dict[str, Any]:
        """Two captures side by side under the laptop's current thresholds: each one's findings and headline
        numbers, then what is only in A, only in B, and how each number changed (B minus A). Use it for
        before/after questions."""
        sess = self._session(session)
        labels = {c["id"]: c.get("label", "") for c in sess.list_captures()}
        snaps = {}
        for cid in (capture_a, capture_b):
            snaps[cid] = capture_snapshot(analyze_capture(self._load(sess, cid), self.ctrl.thresholds))
        a, b = snaps[capture_a], snaps[capture_b]

        def codes(s):
            return [v["code"] for v in s["verdict"]]

        numbers = {}
        for k in ("edges", "revs", "revs_good", "rpm", "ref_sag_mv", "gnd_dev_mv"):
            av, bv = a[k], b[k]
            numbers[k] = {"a": av, "b": bv, "change": None if av is None or bv is None else bv - av}
        return jsonable({
            "session": sess.name,
            "a": {"capture": capture_a, "label": labels[capture_a], **a},
            "b": {"capture": capture_b, "label": labels[capture_b], **b},
            "differences": {"only_in_a": [c for c in codes(a) if c not in codes(b)],
                            "only_in_b": [c for c in codes(b) if c not in codes(a)],
                            "numbers": numbers}})

    def reanalyze_capture(self, capture_id: str, thresholds: dict[str, float] | None = None,
                          session: str | None = None) -> dict[str, Any]:
        """A what-if: analyze a saved capture again with some thresholds changed (for example
        {"ref_sag_mv": 200}). Nothing is saved and the laptop's thresholds are not changed. Valid names are
        the fields of the analysis thresholds (hi, lo, runt_hi, runt_lo, teeth, min_edges, skip_first_edges,
        ref_nominal, ref_sag_mv, ground_mv, event_window_s)."""
        sess = self._session(session)
        data = self._load(sess, capture_id)
        th = dataclasses.replace(self.ctrl.thresholds, **self._overrides(thresholds or {}))
        return {"saved": False, "capture": capture_id, "thresholds": jsonable(dataclasses.asdict(th)),
                **analysis_dict(analyze_capture(data, th))}
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_mcp_tools.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add crank/mcp_tools.py tests/test_mcp_tools.py
git commit -m "Add the MCP compare and what-if analysis tools" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Gated capture and OBD control tools

**Files:**
- Modify: `crank/mcp_tools.py`
- Test: `tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: Task 3's `_gate`, `_obd_state`, `AI_LABEL`; `Controller.arm/cancel/running/connect/disconnect/relink/start_obd/stop_obd/cancel_obd/connection_status`; `crank.obd.transport.SCENARIOS`; `crank.obd.errors.ObdError`.
- Produces: tools `arm_capture(label="", note="")`, `cancel_capture()`, `obd_connect(source)`, `obd_disconnect()`, `obd_relink()`, `obd_read(source="connection")`, `obd_stop()`, `obd_cancel()`. OBD `source` strings are `sim:<scenario>`, `serial:<port or auto>` or (for `obd_read` only) `connection`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_tools.py`:

```python
GATED_CONTROL = [
    lambda t: t.arm_capture(),
    lambda t: t.cancel_capture(),
    lambda t: t.obd_connect("sim:healthy"),
    lambda t: t.obd_disconnect(),
    lambda t: t.obd_relink(),
    lambda t: t.obd_read("sim:healthy"),
    lambda t: t.obd_stop(),
    lambda t: t.obd_cancel(),
]


@pytest.mark.parametrize("call", GATED_CONTROL)
def test_control_tools_refuse_while_ai_control_is_off(ctrl, tools, call):
    with pytest.raises(ToolError, match="AI control is off"):
        call(tools)
    assert ctrl.job is None and ctrl.obd_job is None and ctrl.connection.state == "disconnected"


def test_arm_capture_labels_the_capture_as_ai_and_the_laptop_sees_it(ctrl, tools):
    ctrl.ai_control = True
    ctrl.sync(mode="Simulator", scenario="ref_sag", session_name="bench")
    r = tools.arm_capture("after reground", "note")
    assert r["armed"] is True and r["label"] == "[AI] after reground" and "simulator" in r["message"].lower()
    ctrl.job.join(30)
    p = tools.get_capture_progress()
    assert p["state"] == "done" and p["saved_id"] == "001"
    assert tools.list_captures()["captures"][0]["label"] == "[AI] after reground"
    assert ctrl.case().events()[-1].text == "[AI] after reground"
    assert tools.arm_capture()["label"] == "[AI] capture"             # no label given
    ctrl.job.join(30)


def test_arm_capture_in_real_mode_without_a_scope_says_so(ctrl, tools):
    ctrl.ai_control = True
    ctrl.sync(mode="Real scope")
    with pytest.raises(ToolError, match="Connect to the scope"):
        tools.arm_capture()
    assert ctrl.job is None


def test_arm_while_a_capture_is_running_is_refused_and_cancel_works(ctrl, tools):
    ctrl.ai_control = True
    ctrl.sim_fault = "stuck_waiting"
    ctrl.sync(mode="Simulator", scenario="healthy", session_name="bench")
    tools.arm_capture()
    try:
        with pytest.raises(ToolError, match="already running"):
            tools.arm_capture()
        assert tools.cancel_capture()["cancelled"] is True
    finally:
        ctrl.cancel()
        ctrl.job.join(5)
    assert tools.get_capture_progress()["state"] == "cancelled"
    assert tools.cancel_capture() == {"cancelled": False, "message": "No capture is running."}


def test_turning_control_off_mid_capture_blocks_new_calls_but_not_progress(ctrl, tools):
    ctrl.ai_control = True
    ctrl.sim_fault = "stuck_waiting"
    ctrl.sync(mode="Simulator", scenario="healthy", session_name="bench")
    tools.arm_capture()
    ctrl.ai_control = False
    try:
        assert tools.get_capture_progress()["running"] is True        # still readable
        assert tools.get_status()["ai_control"] is False
        with pytest.raises(ToolError, match="AI control is off"):
            tools.cancel_capture()
        assert ctrl.running                                           # and the capture was not cancelled
    finally:
        ctrl.cancel()
        ctrl.job.join(5)


def test_obd_connect_read_and_disconnect_on_the_simulator(ctrl, tools):
    ctrl.ai_control = True
    ctrl.sync(session_name="bench")
    assert tools.obd_connect("sim:healthy")["state"] == "connected"
    try:
        assert tools.obd_read()["started"] is True                    # default source: the open connection
        ctrl.obd_job.join(30)
        p = tools.obd_progress()
        assert p["state"] == "done" and "get_case_summary" in p["note"]
        assert [e.type for e in ctrl.case().events()].count("code_read") == 1
    finally:
        assert tools.obd_disconnect()["state"] == "disconnected"


def test_obd_read_without_a_connection_uses_its_own_port(ctrl, tools):
    ctrl.ai_control = True
    ctrl.sync(session_name="bench")
    tools.obd_read("sim:healthy")
    ctrl.obd_job.join(30)
    assert tools.obd_progress()["state"] == "done"
    with pytest.raises(ToolError, match="connection is closed"):
        tools.obd_read()                                              # 'connection' but nothing is connected


@pytest.mark.parametrize("source, match", [
    ("bogus", "source must be"), ("sim:", "source must be"), ("serial:", "source must be"),
    ("sim:no_such_scenario", "Unknown simulator scenario"), ("connection", "obd_connect needs"),
])
def test_obd_sources_are_validated(ctrl, tools, source, match):
    ctrl.ai_control = True
    with pytest.raises(ToolError, match=match):
        tools.obd_connect(source)
    assert ctrl.connection.state == "disconnected"


def test_obd_cancel_throws_a_slow_read_away_and_records_nothing(ctrl, tools):
    ctrl.ai_control = True
    ctrl.sync(session_name="bench")
    tools.obd_read("sim:slow_adapter")
    try:
        with pytest.raises(ToolError, match="already running"):
            tools.obd_read("sim:healthy")                             # a second read is refused
    finally:
        assert tools.obd_cancel()["state"] in ("running", "cancelled")
        ctrl.obd_job.join(10)
    assert tools.obd_progress()["state"] == "cancelled"
    assert ctrl.case().events() == []


def test_obd_relink_without_a_connection_explains(ctrl, tools):
    ctrl.ai_control = True
    with pytest.raises(ToolError, match="Nothing to relink"):
        tools.obd_relink()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_tools.py -q -k "gated or control or arm or obd"`
Expected: FAIL (`AttributeError: 'Tools' object has no attribute 'arm_capture'`).

- [ ] **Step 3: Implement**

In `crank/mcp_tools.py`, add imports:

```python
from .obd.errors import ObdError
from .obd.transport import SCENARIOS as OBD_SCENARIOS
```

Add a helper inside `Tools` after `_obd_state`:

```python
    @staticmethod
    def _obd_source(source: str):
        """'sim:<scenario>' -> the scenario name, 'serial:<port|auto>' -> ("serial", value), 'connection' as is."""
        if source == "connection":
            return "connection"
        kind, _, value = source.partition(":")
        if kind == "sim" and value:
            if value not in OBD_SCENARIOS:
                raise ToolError(f"Unknown simulator scenario {value!r}. Scenarios: {', '.join(sorted(OBD_SCENARIOS))}.")
            return value
        if kind == "serial" and value:
            return ("serial", value)
        raise ToolError("source must be 'sim:<scenario>', 'serial:<port or auto>' or 'connection' "
                        "(the open connection).")
```

Add the tools at the end of the class:

```python
    # -- control (need Allow AI control)
    def arm_capture(self, label: str = "", note: str = "") -> dict[str, Any]:
        """Arm the scope for one capture in the laptop's current session, mode, scenario and settings. The
        capture is labeled "[AI] ...". In real-scope mode the scope waits for the first crank edge, so tell
        the owner to crank the engine; this tool only arms. Poll get_capture_progress for the result."""
        self._gate()
        c = self.ctrl
        full = AI_LABEL + (label.strip() or "capture")
        err = c.arm(full, note)
        if err:
            raise ToolError(err)
        sim = c.mode == "Simulator"
        return {"armed": True, "label": full, "session": c.session_name, "mode": c.mode,
                "scenario": c.scenario if sim else None,
                "message": ("Armed in simulator mode: no cranking is needed, the simulated capture finishes by "
                            "itself. Poll get_capture_progress." if sim else
                            "Armed. Tell the owner to crank the engine now; the scope waits for the first crank "
                            "edge. Poll get_capture_progress.")}

    def cancel_capture(self) -> dict[str, Any]:
        """Cancel the running capture and disarm the scope."""
        self._gate()
        if not self.ctrl.running:
            return {"cancelled": False, "message": "No capture is running."}
        self.ctrl.cancel()
        return {"cancelled": True, "message": "Cancel requested; poll get_capture_progress."}

    def obd_connect(self, source: str) -> dict[str, Any]:
        """Hold the OBD adapter open between reads. source is 'sim:<scenario>' (simulator) or
        'serial:<port or auto>' (a real adapter). Returns the connection status."""
        self._gate()
        src = self._obd_source(source)
        if src == "connection":
            raise ToolError("obd_connect needs 'sim:<scenario>' or 'serial:<port or auto>'.")
        try:
            err = self.ctrl.connect(src)
        except ObdError as e:
            raise ToolError(str(e)) from e
        if err:
            raise ToolError(err)
        return jsonable(self.ctrl.connection_status())

    def obd_disconnect(self) -> dict[str, Any]:
        """Close the held adapter connection."""
        self._gate()
        self.ctrl.disconnect()
        return jsonable(self.ctrl.connection_status())

    def obd_relink(self) -> dict[str, Any]:
        """Open a dropped connection again with the same source."""
        self._gate()
        try:
            err = self.ctrl.relink()
        except ObdError as e:
            raise ToolError(str(e)) from e
        if err:
            raise ToolError(err)
        return jsonable(self.ctrl.connection_status())

    def obd_read(self, source: str = "connection") -> dict[str, Any]:
        """Start one read-only OBD-II read (stored, pending and permanent codes, freeze frame, live values)
        into the current case, in the background. source is 'connection' (the open connection, the default),
        'sim:<scenario>' or 'serial:<port or auto>'. Only read requests are ever sent; trouble codes are never
        cleared. Poll obd_progress, then call get_case_summary."""
        self._gate()
        try:
            err = self.ctrl.start_obd(self._obd_source(source))
        except ObdError as e:
            raise ToolError(str(e)) from e
        if err:
            raise ToolError(err)
        return {"started": True, "session": self.ctrl.session_name,
                "message": "Read started in the background. Poll obd_progress; the result goes into the case."}

    def obd_stop(self) -> dict[str, Any]:
        """End the running read and keep what was read so far (acts as cancel before the PCM has answered)."""
        self._gate()
        self.ctrl.stop_obd()
        return jsonable(self._obd_state())

    def obd_cancel(self) -> dict[str, Any]:
        """Throw the running read away; nothing is recorded."""
        self._gate()
        self.ctrl.cancel_obd()
        return jsonable(self._obd_state())
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_mcp_tools.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add crank/mcp_tools.py tests/test_mcp_tools.py
git commit -m "Add the gated MCP capture and OBD control tools" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Gated case-note and suspect tools

**Files:**
- Modify: `crank/mcp_tools.py`
- Test: `tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: Task 1's `Case.append(..., source=)` and `Case.set_hypothesis(..., source=)`; Task 3's `_gate`, `NOTE_TYPES`; Task 4's `_case`.
- Produces: `Tools.add_case_note(type, text)`, `Tools.set_suspect(title, status="open", reason="")`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_tools.py`:

```python
@pytest.mark.parametrize("call", [
    lambda t: t.add_case_note("observation", "x"),
    lambda t: t.set_suspect("Bad PCM"),
])
def test_write_tools_refuse_while_ai_control_is_off(ctrl, tools, call):
    ctrl.sync(session_name="bench")
    with pytest.raises(ToolError, match="AI control is off"):
        call(tools)
    assert ctrl.case().events() == []


def test_add_case_note_records_an_ai_labeled_event(ctrl, tools):
    ctrl.ai_control = True
    ctrl.sync(session_name="bench")
    r = tools.add_case_note("observation", "  5V reference sags to 3.4 V on cranking  ")
    ev = ctrl.case().events()[0]
    assert r == {"id": "e0001", "type": "observation", "session": "bench"}
    assert (ev.type, ev.text, ev.source) == ("observation", "5V reference sags to 3.4 V on cranking", "mcp")
    assert tools.get_case_summary()["timeline"][0]["source"] == "mcp"


def test_add_case_note_is_limited_to_notes(ctrl, tools):
    ctrl.ai_control = True
    with pytest.raises(ToolError, match="type must be one of observation, symptom, change"):
        tools.add_case_note("measurement", "12.6 V")
    with pytest.raises(ToolError, match="needs some text"):
        tools.add_case_note("observation", "   ")
    assert ctrl.case().events() == []


def test_set_suspect_creates_then_updates_by_title(ctrl, tools):
    ctrl.ai_control = True
    ctrl.sync(session_name="bench")
    first = tools.set_suspect("Bad PCM")
    assert first["suspect_id"] == "h1" and first["updated_existing"] is False
    with pytest.raises(ToolError, match="needs a reason"):
        tools.set_suspect("bad pcm", "ruled_out")                    # a status change needs the reason
    second = tools.set_suspect("  bad PCM ", "ruled_out", "second PCM behaved the same")
    assert second["suspect_id"] == "h1" and second["updated_existing"] is True
    assert tools.set_suspect("Chafed harness")["suspect_id"] == "h2"
    board = ctrl.case().hypotheses()
    assert board["h1"].data["status"] == "ruled_out" and board["h1"].source == "mcp"
    with pytest.raises(ToolError, match="unknown hypothesis status"):
        tools.set_suspect("Chafed harness", "maybe")
    with pytest.raises(ToolError, match="needs a title"):
        tools.set_suspect("  ")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_tools.py -q -k "write_tools or case_note or suspect"`
Expected: FAIL (`AttributeError: 'Tools' object has no attribute 'add_case_note'`).

- [ ] **Step 3: Implement**

Add at the end of the `Tools` class in `crank/mcp_tools.py`:

```python
    # -- case writes (need Allow AI control; additions only, marked as AI-authored)
    def add_case_note(self, type: str, text: str) -> dict[str, Any]:
        """Add an observation, symptom or change to the current session's case. It is marked as added by an AI
        assistant. Entries cannot be edited or deleted. Only record what was actually observed or done."""
        self._gate()
        if type not in NOTE_TYPES:
            raise ToolError(f"type must be one of {', '.join(NOTE_TYPES)}.")
        case = self._case(None)
        try:
            ev = case.append(type, text, source="mcp")
        except ValueError as e:
            raise ToolError(str(e)) from e
        return {"id": ev.id, "type": ev.type, "session": case.name}

    def set_suspect(self, title: str, status: str = "open", reason: str = "") -> dict[str, Any]:
        """Add a suspect to the current case's board, or change an existing one (matched by title, ignoring
        case). status is open, ruled_out or confirmed; ruled_out and confirmed need a reason that cites
        evidence. It is marked as added by an AI assistant."""
        self._gate()
        case = self._case(None)
        key = title.strip().lower()
        hid = next((h for h, e in case.hypotheses().items()
                    if str(e.data.get("title", "")).strip().lower() == key), None)
        try:
            ev = case.set_hypothesis(hid, title.strip(), status, reason.strip(), source="mcp")
        except ValueError as e:
            raise ToolError(str(e)) from e
        return {"suspect_id": ev.data["hid"], "title": ev.data["title"], "status": status,
                "updated_existing": hid is not None, "event": ev.id}
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_mcp_tools.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add crank/mcp_tools.py tests/test_mcp_tools.py
git commit -m "Add the gated MCP case note and suspect tools" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 8: The server, its lifecycle, and the end-to-end test

**Files:**
- Create: `crank/mcp_server.py`, `tests/test_mcp_server.py`
- Modify: `crank/controller.py` (`reset_controller` stops the server)

**Interfaces:**
- Consumes: `Tools` (all tool names), `Controller.mcp`.
- Produces:
  - `TOOL_NAMES: tuple[str, ...]`; `INSTRUCTIONS: str`.
  - `build_server(ctrl) -> MCPServer`.
  - `McpStatus(state, url="", message="")` with `state` in `"running"`, `"port_busy"`, `"error"`.
  - `ensure_mcp_server(ctrl, port=None) -> McpStatus` (idempotent per controller; stores a handle on `ctrl.mcp` whose `.status` is the `McpStatus`).
  - `stop_mcp_server(ctrl) -> None`.
  - `DEFAULT_PORT = 8765`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mcp_server.py`:

```python
import asyncio
import socket
import time

import pytest
from mcp import Client

from crank.controller import Controller, get_controller, reset_controller
from crank.mcp_server import TOOL_NAMES, build_server, ensure_mcp_server, stop_mcp_server
from crank.store import SessionStore

EXPECTED_TOOLS = {
    "list_sessions", "get_status", "get_capture_progress", "obd_progress", "get_obd_log", "get_case_summary",
    "get_case_report", "list_captures", "get_capture_analysis", "get_capture_signal", "compare_captures",
    "reanalyze_capture", "arm_capture", "cancel_capture", "obd_connect", "obd_disconnect", "obd_relink",
    "obd_read", "obd_stop", "obd_cancel", "add_case_note", "set_suspect",
}


@pytest.fixture
def ctrl(tmp_path):
    c = Controller(SessionStore(tmp_path / "sessions"), poll_s=0.0, logs_dir=tmp_path / "logs")
    yield c
    stop_mcp_server(c)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _call(c, name, args=None):
    r = await c.call_tool(name, args or {})
    return r


async def _until_done(c, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        p = (await c.call_tool("get_capture_progress", {})).structured_content
        if p["state"] in ("done", "error", "cancelled"):
            return p
        await asyncio.sleep(0.2)
    raise AssertionError("the capture did not finish")


def test_every_tool_is_registered_and_described(ctrl):
    assert set(TOOL_NAMES) == EXPECTED_TOOLS

    async def go():
        async with Client(build_server(ctrl)) as c:
            return (await c.list_tools()).tools
    tools = asyncio.run(go())
    assert {t.name for t in tools} == EXPECTED_TOOLS
    assert all(t.description for t in tools)


def test_gated_tools_report_why_over_the_protocol(ctrl):
    async def go():
        async with Client(build_server(ctrl)) as c:
            return await _call(c, "arm_capture", {"label": "x"})
    r = asyncio.run(go())
    assert r.is_error and "AI control is off" in r.content[0].text
    assert ctrl.job is None


def test_a_simulator_session_end_to_end_through_a_client(ctrl):
    ctrl.ai_control = True
    ctrl.sync(mode="Simulator", scenario="ref_sag", session_name="bench")

    async def go():
        async with Client(build_server(ctrl)) as c:
            assert not (await _call(c, "arm_capture", {"label": "before"})).is_error
            assert (await _until_done(c))["saved_id"] == "001"
            ctrl.sync(scenario="healthy")
            assert not (await _call(c, "arm_capture", {"label": "after"})).is_error
            assert (await _until_done(c))["saved_id"] == "002"
            analysis = (await _call(c, "get_capture_analysis", {"capture_id": "001"})).structured_content
            diff = (await _call(c, "compare_captures",
                                {"capture_a": "001", "capture_b": "002"})).structured_content
            note = await _call(c, "add_case_note", {"type": "observation", "text": "reground made it clean"})
            summary = (await _call(c, "get_case_summary")).structured_content
            return analysis, diff, note, summary
    analysis, diff, note, summary = asyncio.run(go())
    assert analysis["findings"][0]["code"] == "ref_sag"
    assert "ref_sag" in diff["differences"]["only_in_a"] and "signal_clean" in diff["differences"]["only_in_b"]
    assert not note.is_error
    assert [t["source"] for t in summary["timeline"]][-1] == "mcp"
    assert [c["label"] for c in summary["captures"]] == ["[AI] before", "[AI] after"]


def test_the_server_runs_once_on_localhost_and_a_real_client_connects(ctrl):
    port = free_port()
    status = ensure_mcp_server(ctrl, port)
    assert status.state == "running" and status.url == f"http://127.0.0.1:{port}/mcp"
    assert ensure_mcp_server(ctrl, port) is status              # a Streamlit rerun starts nothing new
    assert ctrl.mcp.status is status

    async def go():
        async with Client(status.url) as c:
            return {t.name for t in (await c.list_tools()).tools}
    assert asyncio.run(go()) == EXPECTED_TOOLS
    assert ctrl.mcp.server.config.host == "127.0.0.1"           # localhost only, never the LAN address

    stop_mcp_server(ctrl)
    assert ctrl.mcp is None
    with socket.socket() as s:
        assert s.connect_ex(("127.0.0.1", port)) != 0           # the port is closed again


def test_a_busy_port_degrades_to_a_status_instead_of_raising(ctrl):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        status = ensure_mcp_server(ctrl, port)
    assert status.state == "port_busy" and str(port) in status.message and "CRANK_MCP_PORT" in status.message
    assert ctrl.mcp.server is None and ctrl.mcp.thread is None


def test_port_zero_picks_a_free_port(ctrl):
    status = ensure_mcp_server(ctrl, 0)
    assert status.state == "running" and not status.url.endswith(":0/mcp")


def test_reset_controller_stops_the_server(tmp_path, monkeypatch):
    monkeypatch.setenv("CRANK_SESSIONS_DIR", str(tmp_path))
    monkeypatch.setenv("CRANK_LOGS_DIR", str(tmp_path / "logs"))
    reset_controller()
    ctrl = get_controller()
    status = ensure_mcp_server(ctrl, 0)
    port = int(status.url.split(":")[2].split("/")[0])
    reset_controller()
    with socket.socket() as s:
        assert s.connect_ex(("127.0.0.1", port)) != 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -q`
Expected: FAIL at collection (`ModuleNotFoundError: No module named 'crank.mcp_server'`).

- [ ] **Step 3: Create `crank/mcp_server.py`**

```python
"""The MCP endpoint: tool registration and a localhost-only server thread inside the app process."""
from __future__ import annotations

import os
import socket
import threading
import time
from dataclasses import dataclass

import uvicorn
from mcp.server.mcpserver import MCPServer

from .mcp_tools import Tools

DEFAULT_PORT = 8765
HOST = "127.0.0.1"

TOOL_NAMES = (
    "list_sessions", "get_status", "get_capture_progress", "obd_progress", "get_obd_log",
    "get_case_summary", "get_case_report", "list_captures", "get_capture_analysis", "get_capture_signal",
    "compare_captures", "reanalyze_capture",
    "arm_capture", "cancel_capture", "obd_connect", "obd_disconnect", "obd_relink", "obd_read", "obd_stop",
    "obd_cancel", "add_case_note", "set_suspect",
)

INSTRUCTIONS = """\
Tools for a bench harness that diagnoses a 0 RPM while cranking problem on an LS-swapped truck by recording a
crank signal, its 5V reference and the PCM ground on an oscilloscope, and by reading the PCM over OBD-II.
A verdict is what the harness reported, not a diagnosis: say which area the evidence points to and how
strong it is, and say "insufficient data" when it is. Simulated data is flagged "sim": never treat it as
evidence about the vehicle. Read tools always work. arm_capture, the obd_* control tools, add_case_note and
set_suspect only work while the owner has switched on "Allow AI control". Arming only arms: in real-scope mode
tell the owner to crank the engine. Everything you add to the case is marked as AI-authored; only record what
was actually observed or done."""


@dataclass
class McpStatus:
    state: str              # "running", "port_busy" or "error"
    url: str = ""
    message: str = ""


class McpHandle:
    """What the controller keeps for its server: the status, and the thread and uvicorn server to stop."""

    def __init__(self, status: McpStatus, server=None, thread=None):
        self.status, self.server, self.thread = status, server, thread


def build_server(ctrl) -> MCPServer:
    tools = Tools(ctrl)
    server = MCPServer("ls-swap crank harness", instructions=INSTRUCTIONS)
    for name in TOOL_NAMES:
        server.tool(name=name)(getattr(tools, name))
    return server


def _free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind((HOST, port))
        except OSError:
            return False
    return True


def _pick(port: int) -> int:
    if port:
        return port
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


_lock = threading.Lock()


def ensure_mcp_server(ctrl, port: int | None = None) -> McpStatus:
    """Start this controller's MCP server once and return its status. Calling it again changes nothing, so a
    Streamlit rerun is harmless. A busy port or a failed start is a status, never an exception."""
    with _lock:
        if ctrl.mcp is not None:
            return ctrl.mcp.status
        port = _pick(int(os.environ.get("CRANK_MCP_PORT", DEFAULT_PORT)) if port is None else port)
        url = f"http://{HOST}:{port}/mcp"
        if not _free(port):
            ctrl.mcp = McpHandle(McpStatus("port_busy", url, f"Port {port} is in use. Set CRANK_MCP_PORT to "
                                                              f"another port and restart the app."))
            return ctrl.mcp.status
        try:
            app = build_server(ctrl).streamable_http_app(host=HOST)
            server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=port, log_level="warning"))
            thread = threading.Thread(target=server.run, daemon=True, name="mcp-server")
            thread.start()
            end = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < end:
                time.sleep(0.05)
            if not server.started:
                raise RuntimeError("the server did not start")
        except Exception as e:
            ctrl.mcp = McpHandle(McpStatus("error", url, f"{type(e).__name__}: {e}"))
            return ctrl.mcp.status
        ctrl.mcp = McpHandle(McpStatus("running", url), server, thread)
        return ctrl.mcp.status


def stop_mcp_server(ctrl) -> None:
    """Stop this controller's server (used by tests and when the controller is reset)."""
    with _lock:
        handle, ctrl.mcp = ctrl.mcp, None
    if handle and handle.server is not None:
        handle.server.should_exit = True
        handle.thread.join(5)
```

- [ ] **Step 4: Make `reset_controller` stop the server**

In `crank/controller.py`, replace `reset_controller` with:

```python
def reset_controller() -> None:
    global _controller
    with _guard:
        if _controller is not None and _controller.mcp is not None:
            from .mcp_server import stop_mcp_server           # imported here: mcp_server never imports this module
            stop_mcp_server(_controller)
        _controller = None
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -q`
Expected: all PASS. If `test_every_tool_is_registered_and_described` fails on a missing description, the tool method is missing its docstring.

- [ ] **Step 6: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add crank/mcp_server.py crank/controller.py tests/test_mcp_server.py
git commit -m "Serve the MCP tools on localhost from inside the app process" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 9: The "AI access" sidebar block

**Files:**
- Create: `mcp_ui.py`
- Modify: `app.py` (start the server after `get_controller()`; show the block in the sidebar)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `ensure_mcp_server(ctrl) -> McpStatus`, `Controller.ai_control`.
- Produces: `mcp_ui.ai_panel(ctrl, status)` rendering the status, the URL, the `claude mcp add` line and the **Allow AI control** toggle (`key="ai_control"`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_app.py`:

```python
def test_ai_access_block_shows_the_endpoint_and_starts_with_control_off(at):
    assert at.sidebar.toggle(key="ai_control").value is False
    assert get_controller().ai_control is False
    codes = [c.value for c in at.sidebar.code]
    assert any(c.startswith("claude mcp add --transport http ls-swap http://127.0.0.1:") and c.endswith("/mcp")
               for c in codes)


def test_the_toggle_switches_ai_control_on_and_off(at):
    at.sidebar.toggle(key="ai_control").set_value(True).run()
    assert get_controller().ai_control is True
    at.sidebar.toggle(key="ai_control").set_value(False).run()
    assert get_controller().ai_control is False


def test_another_tab_rerunning_does_not_flip_the_toggle_back(at):
    at.sidebar.toggle(key="ai_control").set_value(True).run()
    assert get_controller().ai_control is True
    get_controller().ai_control = False                   # turned off elsewhere (another tab)
    at.run()                                              # this tab reruns without the user touching the toggle
    assert get_controller().ai_control is False


def test_a_busy_mcp_port_is_shown_not_fatal(tmp_path, monkeypatch):
    import socket
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("CRANK_SESSIONS_DIR", str(tmp_path))
    monkeypatch.setenv("CRANK_POLL_S", "0")
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        monkeypatch.setenv("CRANK_MCP_PORT", str(blocker.getsockname()[1]))
        reset_controller()
        app = AppTest.from_file(APP, default_timeout=60)
        app.run()
        assert not app.exception
        assert any("MCP server is not running" in w.value for w in app.sidebar.warning)
    reset_controller()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_app.py -q -k "ai_access or toggle or mcp_port"`
Expected: FAIL (`KeyError: 'ai_control'` / no toggle found).

- [ ] **Step 3: Create `mcp_ui.py`**

```python
"""The sidebar's "AI access" block: the MCP endpoint, how to add it to Claude Code, and the control toggle."""
import streamlit as st


def _set_ai_control(ctrl) -> None:
    ctrl.ai_control = bool(st.session_state["ai_control"])   # only when this tab's user flips it


def ai_panel(ctrl, status) -> None:
    if status.state == "running":
        st.caption("MCP server (this computer only):")
        st.code(f"claude mcp add --transport http ls-swap {status.url}", language=None)
    else:
        st.warning(f"The MCP server is not running: {status.message}")
    st.toggle("Allow AI control", value=ctrl.ai_control, key="ai_control", on_change=_set_ai_control, args=(ctrl,),
              help="Lets a connected AI assistant arm captures, read the vehicle and add notes to the case. "
                   "Off, it can still read and analyze everything. Resets to off when the app restarts.")
    if ctrl.ai_control:
        st.caption("AI control is on: anything it adds is marked [AI].")
```

- [ ] **Step 4: Wire it into `app.py`**

Add imports next to the existing ones:

```python
from crank.mcp_server import ensure_mcp_server
import mcp_ui
```

Right after `ctrl = get_controller()  # shared by the laptop and phone sessions`, add:

```python
mcp_status = ensure_mcp_server(ctrl)                  # idempotent: one localhost MCP server per app process
```

In the sidebar, after the `conn_ui.connection_panel(ctrl, "obd")` line and before `st.caption("Phone remote (same Wi-Fi):")`, add:

```python
    st.subheader("AI access")
    mcp_ui.ai_panel(ctrl, mcp_status)
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest -q`
Expected: the whole suite PASSES, including the existing `test_app.py` tests.

- [ ] **Step 6: Commit**

```bash
git add mcp_ui.py app.py tests/test_app.py
git commit -m "Add the AI access block to the sidebar" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Documentation, spec alignment and a live smoke test

**Files:**
- Modify: `README.md`, `docs/superpowers/specs/2026-11-19-mcp-server-design.md`

**Interfaces:**
- Consumes: everything above.
- Produces: documentation only.

- [ ] **Step 1: Add a README section**

In `README.md`, add after the "Reading the vehicle (OBD-II)" section (before "## First session at the truck"):

```markdown
### AI access (MCP)

While the app runs it also serves an MCP endpoint on this computer only (`http://127.0.0.1:8765/mcp`; set
`CRANK_MCP_PORT` for another port, restart the app after changing code). The sidebar's **AI access** block shows
the URL and the command to add it to Claude Code:

    claude mcp add --transport http ls-swap http://127.0.0.1:8765/mcp

Any MCP client that speaks streamable HTTP can use it. The endpoint is not reachable from the network and has no
password.

**Always available (read and analyze):** `list_sessions`, `get_status`, `get_case_summary`, `get_case_report`,
`list_captures`, `get_capture_analysis` (ranked findings with evidence), `get_capture_signal` (a downsampled
window of one channel; runts survive the downsampling), `compare_captures`, `reanalyze_capture` (a what-if with
different thresholds; nothing is saved), `get_capture_progress`, `obd_progress`, `get_obd_log`.

**Only while "Allow AI control" is on** (it is off every time the app starts): `arm_capture`, `cancel_capture`,
`obd_connect`, `obd_disconnect`, `obd_relink`, `obd_read`, `obd_stop`, `obd_cancel`, `add_case_note` and
`set_suspect`. They act on the laptop's current session, mode, scenario and settings. Arming only arms: in
Real-scope mode the assistant is told to ask you to crank. OBD reads keep every existing limit (read-only
requests, never clears codes, real and simulated reads never mix in one case).

Everything the assistant causes is labeled. Captures it arms are labeled `[AI] ...`; notes and suspects it adds
carry `source: "mcp"`, show **[AI]** in the timeline and report, and the report says so. It cannot edit or delete
case entries, delete sessions, change the laptop's mode, scenario or thresholds, connect the scope, or send raw
commands. Turning the toggle off mid-capture does not cancel the capture; it only blocks new control calls.

Not checked against a real scope or adapter yet (#3): the control tools drive the same code the buttons do, and
are tested against the simulator only.
```

Add `mcp_ui.py` and the new modules to the "Code layout" list:

```
    crank/mcp_tools.py   the MCP tools: methods over the shared controller (read, gated control and write)
    crank/mcp_server.py  registers the tools and serves them on 127.0.0.1 from inside the app process
    mcp_ui.py            the sidebar's AI access block and the Allow AI control toggle
```
and add `mcp` to the install note if the README lists dependencies (it only references `requirements.txt`, so no change there).

- [ ] **Step 2: Align the spec with what the code does**

Edit `docs/superpowers/specs/2026-11-19-mcp-server-design.md`:
1. Components, `crank/mcp_server.py`: say it builds an `MCPServer` (`from mcp.server.mcpserver import MCPServer`, `mcp` SDK 2.x, streamable HTTP, path `/mcp`), not "FastMCP", and that `build_server(ctrl) -> MCPServer`.
2. Add `crank/mcp_tools.py`: one `Tools` class whose methods are the tools; `mcp_server.py` registers them. Tool failures are the SDK's `ToolError` (a plain exception loses its message over the protocol).
3. `ensure_mcp_server`: the handle lives on `Controller.mcp` (so a module reload cannot lose it), `CRANK_MCP_PORT=0` means a free port, `stop_mcp_server(ctrl)` exists, and `reset_controller()` stops the server.
4. Tools table: `get_capture_progress` and `obd_progress` are always available (they only read state, and the result must stay readable after the toggle goes off); the control table keeps the other eight plus the two write tools.
5. `set_suspect(title, status, reason?)` matches an existing suspect by title (case-insensitive) and updates it, otherwise adds a new one.
6. `get_capture_signal` uses the existing `report.decimate` min/max envelope and reports `nan_samples`.
7. Sidebar: the toggle writes to the controller only when the user flips it (`on_change`), so a stale second tab cannot turn control back on.
8. Dependency: `mcp>=2.3,<3`.

- [ ] **Step 3: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS.

- [ ] **Step 4: Smoke test against the real running app**

Run (in the background): `CRANK_MCP_PORT=8765 .venv/bin/streamlit run app.py --server.headless true --server.port 8501`

Streamlit only runs `app.py` (and so only starts the MCP server) when a browser page connects, so open `http://localhost:8501` in a browser once and check that the sidebar shows the **AI access** block with the `claude mcp add` line and the **Allow AI control** toggle off. Then in another shell:

```bash
.venv/bin/python - <<'EOF'
import asyncio
from mcp import Client

async def main():
    async with Client("http://127.0.0.1:8765/mcp") as c:
        tools = (await c.list_tools()).tools
        print(len(tools), "tools")
        print((await c.call_tool("get_status", {})).structured_content["ai_control"])
        r = await c.call_tool("arm_capture", {})
        print(r.is_error, r.content[0].text)
asyncio.run(main())
EOF
```
Expected: `22 tools`, `False`, then `True AI control is off. Ask the owner ...`. Then switch the toggle on in the browser, rerun the snippet, and expect `False` followed by `Armed in simulator mode...` text with `armed` true (the last line prints `False` for `is_error`); the new capture appears on the Capture page labeled `[AI] capture`. Then stop the app (`pkill -f "streamlit run app.py"`).

Also run `claude mcp add --transport http ls-swap http://127.0.0.1:8765/mcp` and `claude mcp list` to confirm Claude Code shows `ls-swap` as connected while the app is up, then `claude mcp remove ls-swap` to leave the machine as it was.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/superpowers/specs/2026-11-19-mcp-server-design.md
git commit -m "Document the MCP server and align the spec with the code" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```
