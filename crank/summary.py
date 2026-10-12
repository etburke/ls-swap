"""Build a structured, render-agnostic summary of a case. Pure: no I/O beyond reading the case."""
from __future__ import annotations

from dataclasses import dataclass, field

from .case import Case, Event, Vehicle
from .procedure import STEPS

NOT_RECORDED = "not recorded"


@dataclass
class Summary:
    case_name: str
    vehicle: Vehicle
    symptoms: list = field(default_factory=list)        # [{"ts", "text"}] newest first
    timeline: list = field(default_factory=list)        # [{"ts", "type", "line", "synth"}]
    ruled_out: list = field(default_factory=list)       # [{"title", "reason", "evidence"}]
    open: list = field(default_factory=list)            # [{"title", "reason", "evidence"}]
    confirmed: list = field(default_factory=list)
    measurements: list = field(default_factory=list)    # latest per name
    codes: list = field(default_factory=list)           # active codes from the latest code read
    captures: list = field(default_factory=list)        # [{"id","label","ts","sim","snapshot"}]
    before_after: list = field(default_factory=list)    # [{"change","before","after"}]
    not_tried: list = field(default_factory=list)       # guided step titles with no data
    attachments: list = field(default_factory=list)
    skipped_lines: int = 0

    @property
    def empty(self) -> bool:
        return self.vehicle.empty and not self.timeline


def _evidence(refs: dict) -> list:
    out = []
    for k in ("capture", "before", "after", "step"):
        if refs.get(k):
            out.append(f"{k} {refs[k]}")
    return out


def describe(e: Event) -> str:
    d = e.data
    if e.type == "measurement":
        where = f" ({d['where']})" if d.get("where") else ""
        cond = f", {d['conditions']}" if d.get("conditions") else ""
        return f"Measured {d.get('name')}: {d.get('value'):g} {d.get('unit')}{where}{cond}"
    if e.type == "capture":
        snap = d.get("snapshot")
        sim = " [SIMULATED]" if d.get("sim") else ""
        head = (snap["verdict"][0]["title"] if snap and snap.get("verdict") else "verdict " + NOT_RECORDED)
        return f"Capture {e.refs.get('capture', '?')}{sim}: {e.text or 'unlabeled'} — {head}"
    if e.type == "code_read":
        codes = ", ".join(c.get("code", "?") for c in d.get("codes", []))
        return f"Read codes: {codes}"
    if e.type == "hypothesis":
        s = d.get("status", "open").replace("_", " ")
        r = f" — {d['reason']}" if d.get("reason") else ""
        return f"Suspect '{d.get('title')}': {s}{r}"
    label = {"symptom": "Symptom", "observation": "Noted", "change": "Changed"}.get(e.type, e.type)
    return f"{label}: {e.text}"


def build_summary(case: Case) -> Summary:
    events = case.all_events()
    s = Summary(case.name, case.vehicle(), skipped_lines=case.skipped)

    s.symptoms = [{"ts": e.ts, "text": e.text} for e in reversed(events) if e.type == "symptom"]
    s.timeline = [{"ts": e.ts, "type": e.type, "line": describe(e), "synth": e.synth} for e in events]

    for e in case.hypotheses().values():
        row = {"title": e.data.get("title", ""), "reason": e.data.get("reason", ""),
               "evidence": _evidence(e.refs)}
        {"ruled_out": s.ruled_out, "confirmed": s.confirmed}.get(e.data.get("status", "open"), s.open).append(row)

    latest: dict[str, Event] = {}
    for e in events:
        if e.type == "measurement":
            latest[e.data.get("name", "")] = e
    s.measurements = [dict(name=n, value=e.data["value"], unit=e.data.get("unit", ""),
                           where=e.data.get("where", ""), conditions=e.data.get("conditions", ""))
                      for n, e in latest.items()]

    reads = [e for e in events if e.type == "code_read"]
    s.codes = list(reads[-1].data.get("codes", [])) if reads else []

    caps = [e for e in events if e.type == "capture"]
    s.captures = [dict(id=e.refs.get("capture", ""), label=e.text, ts=e.ts,
                       sim=bool(e.data.get("sim")), snapshot=e.data.get("snapshot")) for e in caps]
    by_id = {c["id"]: c for c in s.captures}

    for i, e in enumerate(events):
        if e.type != "change":
            continue
        b, a = e.refs.get("before"), e.refs.get("after")
        if not (b in by_id and a in by_id):             # no explicit link: nearest captures either side
            before = [c for j, c in enumerate(events) if j < i and c.type == "capture"]
            after = [c for j, c in enumerate(events) if j > i and c.type == "capture"]
            if not before or not after:
                continue
            b, a = before[-1].refs.get("capture"), after[0].refs.get("capture")
        s.before_after.append(dict(change=e.text, before=by_id[b], after=by_id[a]))

    have_step = {e.refs.get("step") for e in events if e.refs.get("step")}
    for step in STEPS:
        done = step.id in have_step
        if step.id == "baseline_capture":
            done = done or bool(caps)
        elif step.id == "after_change":
            done = any(e.type == "change" for e in events)
        if not done:
            s.not_tried.append(step.title)

    s.attachments = [f"captures/{c['id']}.npz" for c in s.captures if c["id"]]
    return s
