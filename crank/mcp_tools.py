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
