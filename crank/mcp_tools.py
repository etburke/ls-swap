"""The MCP tools: plain methods over the shared Controller. crank.mcp_server registers them.

Read tools always work. Control and write tools call `_gate()` first and refuse unless the owner has
switched on "Allow AI control". Failures are raised as ToolError so the client sees the message."""
from __future__ import annotations

import dataclasses
import math
from typing import Any

import numpy as np
from mcp.server.mcpserver.exceptions import ToolError

from .analysis import Analysis, analyze_capture
from .case import Case, capture_snapshot
from .model import Thresholds
from .obd.errors import ObdError
from .obd.trafficlog import tail_lines
from .obd.transport import SCENARIOS as OBD_SCENARIOS
from .render import to_html, to_markdown
from .report import decimate
from .summary import build_summary

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
