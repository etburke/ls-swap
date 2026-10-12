"""A case: a session folder plus a vehicle profile and an append-only event log."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .store import Session, _read_json, _write_json

EVENT_TYPES = ("symptom", "observation", "code_read", "measurement", "capture", "change", "hypothesis")
HYPOTHESIS_STATUSES = ("open", "ruled_out", "confirmed")

# What a finding code suggests checking next (used to seed the hypotheses board).
SUGGESTIONS = {
    "no_signal": "Sensor power and ground, or reluctor wheel not turning",
    "harness_mismatch": "Wiring or connector between the sensor and the PCM pin",
    "ref_sag": "5V reference circuit and anything sharing it",
    "ground_lift": "PCM ground path under load",
    "unexplained_dropouts": "Sensor air gap, reluctor wheel, or the sensor itself",
    "no_sync_gap": "Wrong signal or wheel setting",
    "signal_clean": "PCM power, ground or calibration",
}

_lock = threading.RLock()


@dataclass
class Event:
    id: str
    ts: str
    type: str
    text: str = ""
    data: dict = field(default_factory=dict)
    refs: dict = field(default_factory=dict)
    synth: bool = False            # synthesized from legacy files; never written to disk

    def to_json(self) -> str:
        d = asdict(self)
        d.pop("synth")
        return json.dumps(d, separators=(",", ":"))

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        known = {f.name for f in fields(cls)} - {"synth"}
        d = {k: v for k, v in d.items() if k in known}
        for k in ("id", "ts", "type"):
            if not isinstance(d.get(k), str):
                raise ValueError(f"event field {k} must be text")
        if not isinstance(d.get("text", ""), str):
            raise ValueError("event text must be text")
        for k in ("data", "refs"):
            if k in d and not isinstance(d[k], dict):
                raise ValueError(f"event field {k} must be an object")
        return cls(**d)


@dataclass
class Vehicle:
    year: str = ""
    make: str = ""
    model: str = ""
    engine: str = ""
    transmission: str = ""
    mileage: str = ""
    modifications: str = ""
    vin: str = ""
    problem: str = ""

    @property
    def empty(self) -> bool:
        return not any(asdict(self).values())

    @property
    def title(self) -> str:
        t = " ".join(x for x in (self.year, self.make, self.model) if x)
        return t or "Vehicle not recorded"


def load_events(path: Path) -> tuple[list[Event], int]:
    """Read events.jsonl; damaged lines are skipped and counted."""
    events, skipped = [], 0
    try:
        lines = path.read_bytes().decode("utf-8", "replace").splitlines()
    except FileNotFoundError:
        return [], 0
    for line in lines:
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            if not isinstance(d, dict):
                raise ValueError
            events.append(Event.from_dict(d))
        except (ValueError, TypeError):
            skipped += 1
    return events, skipped


def _validate(type_: str, text: str, data: dict) -> None:
    if type_ not in EVENT_TYPES:
        raise ValueError(f"unknown event type {type_!r}")
    if type_ == "measurement":
        try:
            data["value"] = float(data["value"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("a measurement needs a numeric value") from None
        if not str(data.get("unit", "")).strip():
            raise ValueError("a measurement needs a unit")
        if not str(data.get("name", "")).strip():
            raise ValueError("a measurement needs a name")
    elif type_ == "hypothesis":
        if not str(data.get("title", "")).strip():
            raise ValueError("a hypothesis needs a title")
        status = data.get("status", "open")
        if status not in HYPOTHESIS_STATUSES:
            raise ValueError(f"unknown hypothesis status {status!r}")
        if status != "open" and not str(data.get("reason", "")).strip():
            raise ValueError("changing a hypothesis to ruled out or confirmed needs a reason")
    elif type_ in ("symptom", "observation", "change") and not text.strip():
        raise ValueError(f"a {type_} needs some text")
    elif type_ == "code_read" and not data.get("codes") and data.get("source") != "obd":
        raise ValueError("a code read needs at least one code")


def capture_snapshot(analysis) -> dict | None:
    """Freeze the verdict and headline numbers so reports do not depend on later analysis changes."""
    if analysis is None:
        return None
    r = analysis.reports.get("ch1") or next(iter(analysis.reports.values()), None)
    order = {"fail": 0, "warn": 1, "info": 2}
    sag, gnd = analysis.supply.get("ref_sag_mv"), analysis.supply.get("gnd_dev_mv")
    return {
        "verdict": [{"code": f.code, "severity": f.severity, "title": f.title}
                    for f in sorted(analysis.findings, key=lambda f: order[f.severity])],
        "edges": r.n_edges if r else None,
        "revs_good": r.revs_good if r else None,
        "revs": r.revs if r else None,
        "rpm": round(r.rpm_median) if r and r.rpm_median else None,
        "ref_sag_mv": None if sag is None else round(sag),     # None = channel not captured
        "gnd_dev_mv": None if gnd is None else round(gnd),
    }


class Case:
    def __init__(self, session: Session):
        self.session = session
        self.path = session.path
        self.name = session.name
        self.skipped = 0

    # -- vehicle
    def vehicle(self) -> Vehicle:
        d = _read_json(self.path / "vehicle.json", {})
        if not isinstance(d, dict):
            return Vehicle()
        known = {f.name for f in fields(Vehicle)}
        return Vehicle(**{k: str(v) for k, v in d.items() if k in known})

    def set_vehicle(self, v: Vehicle) -> None:
        _write_json(self.path / "vehicle.json", asdict(v))

    # -- events
    @property
    def _events_path(self) -> Path:
        return self.path / "events.jsonl"

    def events(self) -> list[Event]:
        """Events from the log only (no legacy synthesis)."""
        events, self.skipped = load_events(self._events_path)
        return events

    def append(self, type_: str, text: str = "", data: dict | None = None,
               refs: dict | None = None) -> Event:
        data, refs = dict(data or {}), dict(refs or {})
        _validate(type_, text, data)
        with _lock:                               # id assignment and the write are one step
            existing, _ = load_events(self._events_path)
            nums = [int(e.id[1:]) for e in existing if e.id[:1] == "e" and e.id[1:].isdigit()]
            ev = Event(f"e{(max(nums) + 1) if nums else 1:04d}", time.strftime("%Y-%m-%dT%H:%M:%S"),
                       type_, text.strip(), data, refs)
            lead = ""
            try:
                with open(self._events_path, "rb") as f:          # a crash can leave a partial last line
                    f.seek(-1, 2)
                    lead = "" if f.read(1) == b"\n" else "\n"
            except OSError:
                pass
            with open(self._events_path, "a") as f:
                f.write(lead + ev.to_json() + "\n")
        return ev

    def append_capture(self, capture_id: str, label: str, analysis, sim: bool) -> Event:
        return self.append("capture", label, {"snapshot": capture_snapshot(analysis), "sim": sim},
                           {"capture": capture_id})

    def append_obd(self, readout) -> list[Event]:
        """Record one OBD read: a code_read (empty list = a clean read) plus a measurement per live value."""
        out = [self.append("code_read", "", readout.to_event_data())]
        rpm = next((v.value for v in readout.live if v.name == "Engine RPM"), None)
        cond = "OBD, PCM reported no RPM" if rpm is None else f"OBD, PCM reports {rpm:g} RPM"
        for v in readout.live:
            if v.value is None:
                continue
            out.append(self.append("measurement", "", {
                "name": v.name, "value": v.value, "unit": v.unit, "where": "OBD live data",
                "conditions": cond, "sim": readout.sim}, {"obd": out[0].id}))
        return out

    def log_guided_step(self, step, values: dict) -> list[Event]:
        """Turn a saved guided-test step into events."""
        out = []
        for f in step.fields:
            v = values.get(f.key)
            if v is None:                                  # left unset: nothing was measured
                continue
            if f.kind == "number":
                out.append(self.append("measurement", "", {
                    "name": f.label, "value": float(v), "unit": f.unit, "where": step.title,
                    "conditions": ""}, {"step": step.id}))
            elif f.kind == "bool":
                out.append(self.append("observation", f"{f.label}: {'yes' if v else 'no'}",
                                       {}, {"step": step.id}))
            elif f.kind == "text" and str(v).strip():
                out.append(self.append("observation", f"{f.label}: {str(v).strip()}",
                                       {}, {"step": step.id}))
        return out

    def hypotheses(self) -> dict[str, Event]:
        """Latest event per hypothesis id."""
        latest: dict[str, Event] = {}
        for e in self.all_events():
            if e.type == "hypothesis" and e.data.get("hid"):
                latest[e.data["hid"]] = e
        return latest

    def set_hypothesis(self, hid: str | None, title: str, status: str = "open",
                       reason: str = "", refs: dict | None = None) -> Event:
        with _lock:                                        # allocate the id and write as one step
            if not hid:
                nums = [int(h[1:]) for h in self.hypotheses() if h[:1] == "h" and h[1:].isdigit()]
                hid = f"h{(max(nums) + 1) if nums else 1}"
            return self.append("hypothesis", "", {"hid": hid, "title": title, "status": status,
                                                  "reason": reason}, refs)

    # -- everything, including legacy files read as events
    def all_events(self) -> list[Event]:
        real = self.events()
        have_capture = {e.refs.get("capture") for e in real if e.type == "capture"}
        have_step = {e.refs.get("step") for e in real if e.refs.get("step")}
        extra: list[Event] = []
        for i, c in enumerate(self.session.list_captures()):
            if c["id"] in have_capture:
                continue
            extra.append(Event(f"legacy-c{c['id']}", (c.get("time") or "").replace(" ", "T"), "capture",
                               c.get("label", ""), {"snapshot": None, "sim": bool(c.get("sim"))},
                               {"capture": c["id"]}, synth=True))
        from .procedure import STEPS
        readings = self.session.readings()
        for step in STEPS:
            vals = readings.get(step.id)
            if not isinstance(vals, dict) or step.id in have_step:
                continue
            for f in step.fields:
                if f.key not in vals:
                    continue
                v = vals[f.key]
                if v is None:
                    continue
                if f.kind == "number":
                    extra.append(Event(f"legacy-{step.id}-{f.key}", "", "measurement", "",
                                       {"name": f.label, "value": v, "unit": f.unit,
                                        "where": step.title, "conditions": ""},
                                       {"step": step.id}, synth=True))
                elif f.kind == "bool":
                    extra.append(Event(f"legacy-{step.id}-{f.key}", "", "observation",
                                       f"{f.label}: {'yes' if v else 'no'}", {}, {"step": step.id}, synth=True))
                elif f.kind == "text" and str(v).strip():
                    extra.append(Event(f"legacy-{step.id}-{f.key}", "", "observation",
                                       f"{f.label}: {str(v).strip()}", {}, {"step": step.id}, synth=True))
        notes = self.session.notes()
        if notes.strip():
            extra.append(Event("legacy-notes", "", "observation", notes.strip(), {}, {}, synth=True))
        # real events keep log order; dated legacy items slot in by time, undated ones come first
        undated = [x for x in extra if not x.ts]
        dated = sorted((x for x in extra if x.ts), key=lambda x: x.ts)
        merged, i = [], 0
        for r in real:
            while i < len(dated) and dated[i].ts <= r.ts:
                merged.append(dated[i]); i += 1
            merged.append(r)
        return undated + merged + dated[i:]


def suggest_hypotheses(codes, existing_titles=()) -> list[str]:
    """Suggested suspects for a verdict's finding codes, minus ones already on the board."""
    have = {t.strip().lower() for t in existing_titles}
    out = []
    for c in codes:
        s = SUGGESTIONS.get(c)
        if s and s.lower() not in have and s not in out:
            out.append(s)
    return out
