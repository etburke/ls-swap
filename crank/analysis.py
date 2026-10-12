"""Pure analysis of crank captures: edges, gaps, runts, supply correlation, verdict.

No scope, disk, or UI access. Everything takes arrays and a Thresholds object.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .model import CaptureData, Thresholds


@dataclass
class SignalReport:
    name: str
    label: str
    n_edges: int = 0
    edges: np.ndarray = field(default_factory=lambda: np.empty(0))
    usable: bool = False
    gaps: int = 0
    missing: int = 0
    extra: int = 0
    runt_hi: int = 0
    runt_lo: int = 0
    high_median: float | None = None
    high_lowest: float | None = None
    revs: int = 0
    revs_good: int = 0
    rpm_median: float | None = None
    rpm_min: float | None = None
    rpm_max: float | None = None
    bad_revs: list = field(default_factory=list)      # (time, edge_count)
    events: list = field(default_factory=list)        # (time, kind)

    @property
    def anomalies(self) -> int:
        return self.missing + self.extra + self.runt_hi + self.runt_lo + len(self.bad_revs)


@dataclass
class Finding:
    code: str
    severity: str          # "info" | "warn" | "fail"
    title: str
    detail: str
    evidence: dict = field(default_factory=dict)


@dataclass
class Analysis:
    reports: dict
    supply: dict
    compare: dict | None
    correlations: list
    findings: list

    @property
    def codes(self) -> set:
        return {f.code for f in self.findings}

    @property
    def events(self) -> list:
        return sorted(e for r in self.reports.values() for e in r.events)


def schmitt(v: np.ndarray, lo: float, hi: float) -> np.ndarray:
    above, below = v > hi, v < lo
    idx = np.flatnonzero(above | below)
    if idx.size == 0:
        return np.zeros(v.size, bool)
    st = above[idx]
    pos = np.searchsorted(idx, np.arange(v.size), side="right") - 1
    return np.where(pos >= 0, st[np.clip(pos, 0, None)], st[0])


def analyze_signal(name: str, label: str, t: np.ndarray, v: np.ndarray,
                   th: Thresholds) -> SignalReport:
    s = schmitt(v, th.lo, th.hi)
    rise = np.flatnonzero(~s[:-1] & s[1:]) + 1
    rep = SignalReport(name=name, label=label, n_edges=len(rise), edges=t[rise])
    if len(rise) < th.min_edges:
        return rep
    rep.usable = True
    tr = t[rise]
    per = np.diff(tr)
    ratio = np.ones_like(per)
    for i in range(len(per)):
        lo_i = max(0, i - 4)
        nb = np.delete(per[lo_i:i + 5], i - lo_i)
        ratio[i] = per[i] / np.median(nb)
    ratio[:th.skip_first_edges] = 1.0
    gaps = np.flatnonzero(ratio > 2.5)
    drops = np.flatnonzero((ratio > 1.6) & (ratio <= 2.5))
    extra = np.flatnonzero(ratio < 0.6)

    # runt / weak pulses: cross the midpoint but never reach the full levels
    mid = (th.hi + th.lo) / 2
    m = v > mid
    starts = np.concatenate(([0], np.flatnonzero(m[1:] != m[:-1]) + 1))
    vmax = np.maximum.reduceat(v, starts)
    vmin = np.minimum.reduceat(v, starts)
    state = m[starts]
    inner = lambda ix: ix[(ix > 0) & (ix < len(starts) - 1)]
    runt_hi = inner(np.flatnonzero(state & (vmax < th.runt_hi)))
    runt_lo = inner(np.flatnonzero(~state & (vmin > th.runt_lo)))

    rep.gaps, rep.missing, rep.extra = len(gaps), len(drops), len(extra)
    rep.runt_hi, rep.runt_lo = len(runt_hi), len(runt_lo)
    highs = vmax[state]
    if highs.size:
        rep.high_median, rep.high_lowest = float(np.median(highs)), float(highs.min())

    if len(gaps) >= 2:
        counts = np.diff(gaps)
        revt = tr[gaps[1:] + 1] - tr[gaps[:-1] + 1]
        rpm = 60.0 / revt
        bad = np.flatnonzero(counts != th.teeth)
        rep.revs, rep.revs_good = len(counts), len(counts) - len(bad)
        rep.rpm_median, rep.rpm_min, rep.rpm_max = (
            float(np.median(rpm)), float(rpm.min()), float(rpm.max()))
        rep.bad_revs = [(float(tr[gaps[j] + 1]), int(counts[j])) for j in bad]

    ev = [(float(tr[i + 1]), "missing edge?") for i in drops]
    ev += [(float(tr[i + 1]), "extra edge") for i in extra]
    ev += [(float(t[starts[i]]), "runt high") for i in runt_hi]
    ev += [(float(t[starts[i]]), "weak low") for i in runt_lo]
    rep.events = sorted(ev)
    return rep


def supply_stats(t: np.ndarray, chans: dict, th: Thresholds, window) -> dict:
    mask = (t >= window[0]) & (t <= window[1]) if window else np.ones(t.size, bool)
    out: dict = {"window": window}
    if 3 in chans:
        v = chans[3][mask]
        p1 = float(np.percentile(v, 1))
        out.update(ref_p1=p1, ref_median=float(np.median(v)),
                   ref_sag_mv=max(0.0, th.ref_nominal - p1) * 1000)
    if 4 in chans:
        v = chans[4][mask]
        p99 = float(np.percentile(np.abs(v), 99))
        out.update(gnd_p99=p99, gnd_dev_mv=p99 * 1000)
    return out


def compare_edges(a: SignalReport, b: SignalReport) -> dict | None:
    if a.n_edges < 2 or b.n_edges < 2:
        return None
    a1, a2 = a.edges, b.edges
    j = np.clip(np.searchsorted(a1, a2), 1, len(a1) - 1)
    near = np.where(np.abs(a1[j] - a2) < np.abs(a1[j - 1] - a2), a1[j], a1[j - 1])
    dly = near - a2
    return dict(n1=a.n_edges, n2=b.n_edges,
                delay_median_us=float(np.median(dly) * 1e6),
                delay_worst_us=float(np.abs(dly).max() * 1e6))


def correlate_events(t, chans, events, th: Thresholds, limit=500) -> list:
    if not events or (3 not in chans and 4 not in chans):
        return []
    dt = float(np.median(np.diff(t[:1000])))
    w = max(1, int(th.event_window_s / dt))
    out = []
    for te, kind in events[:limit]:
        i = int(np.searchsorted(t, te))
        row = dict(t=te, kind=kind, ref_dev_mv=0.0, gnd_dev_mv=0.0)
        if 3 in chans:
            seg = chans[3][max(0, i - w):i + w + 1] - th.ref_nominal
            row["ref_dev_mv"] = float(seg[np.argmax(np.abs(seg))] * 1000)
        if 4 in chans:
            seg = chans[4][max(0, i - w):i + w + 1]
            row["gnd_dev_mv"] = float(seg[np.argmax(np.abs(seg))] * 1000)
        row["aligned"] = bool(abs(row["ref_dev_mv"]) > 0.5 * th.ref_sag_mv
                              or abs(row["gnd_dev_mv"]) > 0.5 * th.ground_mv)
        out.append(row)
    return out


def build_findings(reports, supply, cmp, corr, th: Thresholds) -> list:
    F: list[Finding] = []

    def add(code, sev, title, detail, **ev):
        F.append(Finding(code, sev, title, detail, ev))

    r1, r2 = reports.get("ch1"), reports.get("ch2")
    live = [r for r in (r1, r2) if r is not None]
    if not live:
        add("insufficient_data", "warn", "No crank channels in this capture",
            "Enable CH1 (crank at the PCM pin) and/or CH2 (crank at the sensor connector).")
        return F

    usable = [r for r in live if r.usable]
    ref_sag = supply.get("ref_sag_mv", 0.0) >= th.ref_sag_mv
    gnd_lift = supply.get("gnd_dev_mv", 0.0) >= th.ground_mv
    anomalous = any(r.usable and r.anomalies >= max(3, 0.01 * r.n_edges)
                    for r in reports.values())
    aligned = [c for c in corr if c["aligned"]]
    align_txt = (f" {len(aligned)} of {len(corr)} dropout events coincide with a supply/ground "
                 f"disturbance." if corr else "")

    if not usable:
        names = " and ".join(r.label for r in live)
        add("no_signal", "fail", "No usable crank signal",
            f"{names}: fewer than {th.min_edges} clean rising edges. With the sensor wiring "
            f"verified, check the sensor's 5V reference, its ground, and whether the "
            f"reluctor is turning.", edges={r.name: r.n_edges for r in live})
    elif len(live) == 2 and r1.usable != r2.usable:
        dead = r1 if not r1.usable else r2
        add("harness_mismatch", "fail",
            f"{dead.label} has no usable signal but the other crank channel does",
            "The same signal looks different at the two probe points: suspect the wiring "
            "or connector between them (or a probe problem on the dead channel).",
            edges={r.name: r.n_edges for r in live})
    elif cmp and abs(cmp["n1"] - cmp["n2"]) > 0.05 * max(cmp["n1"], cmp["n2"]):
        add("harness_mismatch", "fail",
            "Crank edge counts differ between PCM pin and sensor connector",
            f"{cmp['n1']} vs {cmp['n2']} rising edges: suspect the harness or connector.",
            **cmp)

    if ref_sag:
        add("ref_sag", "fail" if anomalous or not usable else "warn",
            f"5V reference sags {supply['ref_sag_mv']:.0f} mV during cranking",
            f"Lowest 5V ref (1st percentile) is {supply['ref_p1']:.2f} V." + align_txt,
            ref_sag_mv=supply["ref_sag_mv"])
    if gnd_lift:
        add("ground_lift", "fail" if anomalous or not usable else "warn",
            f"PCM low reference deviates {supply['gnd_dev_mv']:.0f} mV from battery negative",
            "The PCM's ground lifts relative to the battery during cranking, so the signal it "
            "sees (CH1 - CH4) is shifted." + align_txt, gnd_dev_mv=supply["gnd_dev_mv"])
    if usable and anomalous and not (ref_sag or gnd_lift):
        worst = max(usable, key=lambda r: r.anomalies)
        add("unexplained_dropouts", "fail",
            "Dropouts or runt pulses with a steady supply and ground",
            f"{worst.label}: {worst.missing} missing-edge suspects, {worst.runt_hi} runt highs, "
            f"{worst.runt_lo} weak lows, {len(worst.bad_revs)} revolutions with the wrong edge "
            f"count. 5V ref and PCM ground were steady, so suspect the sensor, its air gap, "
            f"or the reluctor wheel.", anomalies=worst.anomalies)
    if usable and not any(r.gaps >= 2 for r in usable):
        add("no_sync_gap", "warn", "No 58x sync gap found",
            "Fewer than 2 gaps were found, so edges per revolution cannot be counted. Check "
            "the teeth setting, that this is the crank (not cam) signal, and the reluctor wheel.")

    if not any(f.severity in ("warn", "fail") for f in F):
        add("signal_clean", "info", "Crank signal looks clean at the scope",
            "Edges, gaps, levels, 5V reference and PCM ground all look good in this capture. "
            "If the PCM still shows 0 RPM, look at the PCM side: power, ground, calibration.")
    return F


def analyze_capture(data: CaptureData, th: Thresholds | None = None) -> Analysis:
    th = th or Thresholds()
    t, ch = data.t, data.channels
    reports: dict[str, SignalReport] = {}
    for n in (1, 2):
        if n in ch:
            reports[f"ch{n}"] = analyze_signal(f"ch{n}", data.labels.get(n, f"CH{n}"), t, ch[n], th)
    if 1 in ch and 4 in ch:
        reports["ch1_rel"] = analyze_signal("ch1_rel", "Crank as PCM sees it (CH1-CH4)",
                                            t, ch[1] - ch[4], th)
    live = [reports[k].edges for k in ("ch1", "ch2") if k in reports and reports[k].usable]
    window = (min(e[0] for e in live), max(e[-1] for e in live)) if live else None
    supply = supply_stats(t, ch, th, window)
    cmp = (compare_edges(reports["ch1"], reports["ch2"])
           if "ch1" in reports and "ch2" in reports else None)
    events = sorted(e for r in reports.values() for e in r.events)
    corr = correlate_events(t, ch, events, th)
    return Analysis(reports, supply, cmp, corr, build_findings(reports, supply, cmp, corr, th))
