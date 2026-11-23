"""Pure analysis of crank captures: edges, gaps, runts, supply correlation, verdict.

No scope, disk, or UI access. Everything takes arrays and a Thresholds object.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .model import CaptureData, Thresholds
from .roles import ROLES, role_label, roles_of


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
    rev_rpm: list = field(default_factory=list)       # (mid-time, rpm) of each complete revolution
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
    roles: dict = field(default_factory=dict)         # channel -> (role, num)
    basic: dict = field(default_factory=dict)         # "chN" -> min/median/max/pulses for non-analysed roles

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
        start, end = tr[gaps[:-1] + 1], tr[gaps[1:] + 1]
        rep.rev_rpm = [(float((start[j] + end[j]) / 2), float(rpm[j])) for j in range(len(counts))
                       if counts[j] == th.teeth]

    ev = [(float(tr[i + 1]), "missing edge?") for i in drops]
    ev += [(float(tr[i + 1]), "extra edge") for i in extra]
    ev += [(float(t[starts[i]]), "runt high") for i in runt_hi]
    ev += [(float(t[starts[i]]), "weak low") for i in runt_lo]
    rep.events = sorted(ev)
    return rep


def supply_stats(t: np.ndarray, chans: dict, th: Thresholds, window, refs=(3,), lows=(4,)) -> dict:
    """5V reference sag over the `refs` channels and ground deviation over the `lows` channels, each the worst
    channel, named by ref_channel / gnd_channel."""
    mask = (t >= window[0]) & (t <= window[1]) if window else np.ones(t.size, bool)
    out: dict = {"window": window}
    for n in refs:
        if n not in chans:
            continue
        v = chans[n][mask]
        v = v[np.isfinite(v)]
        if v.size:
            p1 = float(np.percentile(v, 1))
            if "ref_p1" not in out or p1 < out["ref_p1"]:
                out.update(ref_p1=p1, ref_median=float(np.median(v)),
                           ref_sag_mv=max(0.0, th.ref_nominal - p1) * 1000, ref_channel=n)
    for n in lows:
        if n not in chans:
            continue
        v = chans[n][mask]
        v = v[np.isfinite(v)]
        if v.size:
            p99 = float(np.percentile(np.abs(v), 99))
            if "gnd_p99" not in out or p99 > out["gnd_p99"]:
                out.update(gnd_p99=p99, gnd_dev_mv=p99 * 1000, gnd_channel=n)
    return out


def basic_stats(t: np.ndarray, v: np.ndarray) -> dict:
    """Plain numbers for a channel no check applies to: levels, and pulses counted with a Schmitt trigger at
    30% / 70% of its 1st-99th percentile span."""
    ok = np.isfinite(v)
    if not ok.any():
        return {"min": None, "median": None, "max": None, "pulses": 0, "pulse_width_ms_median": None}
    x = v[ok]
    p1, p99 = float(np.percentile(x, 1)), float(np.percentile(x, 99))
    out = {"min": float(x.min()), "median": float(np.median(x)), "max": float(x.max()), "pulses": 0,
           "pulse_width_ms_median": None}
    span = p99 - p1
    if span < 0.2:                                    # flat: no pulses worth counting
        return out
    s = schmitt(np.where(ok, v, p1), p1 + 0.3 * span, p1 + 0.7 * span)
    rise = np.flatnonzero(~s[:-1] & s[1:]) + 1
    fall = np.flatnonzero(s[:-1] & ~s[1:]) + 1
    out["pulses"] = int(len(rise))
    widths = [t[fall[j]] - t[r] for r in rise for j in [np.searchsorted(fall, r)] if j < len(fall)]
    if len(widths) >= 2:
        out["pulse_width_ms_median"] = float(np.median(widths) * 1000)
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


def correlate_events(t, chans, events, th: Thresholds, limit=500, ref: int | None = 3,
                     gnd: int | None = 4) -> list:
    if not events or (ref not in chans and gnd not in chans):
        return []
    dt = float(np.median(np.diff(t[:1000])))
    w = max(1, int(th.event_window_s / dt))
    out = []
    for te, kind in events[:limit]:
        i = int(np.searchsorted(t, te))
        row = dict(t=te, kind=kind, ref_dev_mv=0.0, gnd_dev_mv=0.0)
        if ref in chans:
            seg = chans[ref][max(0, i - w):i + w + 1] - th.ref_nominal
            row["ref_dev_mv"] = float(seg[np.argmax(np.abs(seg))] * 1000)
        if gnd in chans:
            seg = chans[gnd][max(0, i - w):i + w + 1]
            row["gnd_dev_mv"] = float(seg[np.argmax(np.abs(seg))] * 1000)
        row["aligned"] = bool(abs(row["ref_dev_mv"]) > 0.5 * th.ref_sag_mv
                              or abs(row["gnd_dev_mv"]) > 0.5 * th.ground_mv)
        out.append(row)
    return out


def build_findings(reports, supply, cmp, corr, th: Thresholds, crank: list | None = None,
                   pair: tuple | None = None, names: dict | None = None) -> list:
    """`crank`: the crank-signal report keys; `pair`: (ECM key, sensor key) for the two-point comparison;
    `names`: {"ref": label, "gnd": label, "rel": label} for titles. Defaults are the legacy layout."""
    crank = crank if crank is not None else [k for k in ("ch1", "ch2") if k in reports]
    pair = pair if pair is not None else ("ch1", "ch2")
    names = {"ref": "5V reference", "gnd": "PCM low reference", "rel": "CH1 - CH4", **(names or {})}
    F: list[Finding] = []

    def add(code, sev, title, detail, **ev):
        F.append(Finding(code, sev, title, detail, ev))

    r1, r2 = reports.get(pair[0]), reports.get(pair[1])
    live = [reports[k] for k in crank]
    if not live:
        add("insufficient_data", "warn", "No crank channels in this capture",
            "Set a channel's role to Crank signal at ECM or Crank signal at sensor.")
        return F

    usable = [r for r in live if r.usable]
    ref_sag = supply.get("ref_sag_mv", 0.0) >= th.ref_sag_mv
    gnd_lift = supply.get("gnd_dev_mv", 0.0) >= th.ground_mv
    anomalous = any(r.usable and r.anomalies >= max(3, 0.01 * r.n_edges)
                    for r in reports.values())          # many: severity "fail"
    any_anomaly = any(r.usable and r.anomalies > 0 for r in reports.values())
    aligned = [c for c in corr if c["aligned"]]
    align_txt = (f" {len(aligned)} of {len(corr)} dropout events coincide with a supply/ground "
                 f"disturbance." if corr else "")

    if not usable:
        names = " and ".join(r.label for r in live)
        add("no_signal", "fail", "No usable crank signal",
            f"{names}: fewer than {th.min_edges} clean rising edges. With the sensor wiring "
            f"verified, check the sensor's 5V reference, its ground, and whether the "
            f"reluctor is turning.", edges={r.name: r.n_edges for r in live})
    elif r1 is not None and r2 is not None and r1.usable != r2.usable:
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
            f"{names['ref']} sags {supply['ref_sag_mv']:.0f} mV during cranking",
            f"Lowest 5V ref (1st percentile) is {supply['ref_p1']:.2f} V." + align_txt,
            ref_sag_mv=supply["ref_sag_mv"])
    if gnd_lift:
        add("ground_lift", "fail" if anomalous or not usable else "warn",
            f"{names['gnd']} deviates {supply['gnd_dev_mv']:.0f} mV from battery negative",
            "The PCM's ground lifts relative to the battery during cranking, so the signal it "
            f"sees ({names['rel']}) is shifted." + align_txt, gnd_dev_mv=supply["gnd_dev_mv"])
    if usable and any_anomaly and not (ref_sag or gnd_lift):
        worst = max(usable, key=lambda r: r.anomalies)
        add("unexplained_dropouts", "fail" if anomalous else "warn",
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
    roles = roles_of(data)
    kind = {n: ROLES[r].kind for n, (r, _) in roles.items()}
    first = lambda key: next((n for n in sorted(roles) if roles[n][0] == key), None)
    label = lambda n: data.labels.get(n, f"CH{n}")
    crank_ch = [n for n in sorted(ch) if kind[n] == "crank"]
    reports: dict[str, SignalReport] = {}
    for n in crank_ch:
        reports[f"ch{n}"] = analyze_signal(f"ch{n}", label(n), t, ch[n], th)
    ecm, sen, low = first("crank_ecm"), first("crank_sensor"), first("crank_low")
    rel = f"CH{ecm} - CH{low}" if ecm and low else "the crank signal against the ECM's ground"
    if ecm and low:
        reports["ecm_rel"] = analyze_signal("ecm_rel", f"Crank as the ECM sees it (CH{ecm}-CH{low})",
                                            t, ch[ecm] - ch[low], th)
    live = [reports[f"ch{n}"].edges for n in crank_ch if reports[f"ch{n}"].usable]
    window = (min(e[0] for e in live), max(e[-1] for e in live)) if live else None
    supply = supply_stats(t, ch, th, window, refs=[n for n in sorted(ch) if kind[n] == "ref"],
                          lows=[n for n in sorted(ch) if kind[n] == "low"])
    pair = (f"ch{ecm}", f"ch{sen}") if ecm and sen else ("", "")
    cmp = compare_edges(reports[pair[0]], reports[pair[1]]) if ecm and sen else None
    events = sorted(e for r in reports.values() for e in r.events)
    corr = correlate_events(t, ch, events, th, ref=supply.get("ref_channel"), gnd=supply.get("gnd_channel"))
    names = {"rel": rel}
    refs_n = sum(k == "ref" for k in kind.values())
    lows_n = sum(k == "low" for k in kind.values())
    # name the role only when it is not the classic crank reference / ground, or there was a choice of channels
    if "ref_channel" in supply and (refs_n > 1 or roles[supply["ref_channel"]][0] != "crank_ref"):
        names["ref"] = role_label(*roles[supply["ref_channel"]])
    if "gnd_channel" in supply and (lows_n > 1 or roles[supply["gnd_channel"]][0] != "crank_low"):
        names["gnd"] = role_label(*roles[supply["gnd_channel"]])
    findings = build_findings(reports, supply, cmp, corr, th, crank=[f"ch{n}" for n in crank_ch],
                              pair=pair, names=names)
    basic = {f"ch{n}": basic_stats(t, ch[n]) for n in sorted(ch) if kind[n] == "basic"}
    bad = {n: int((~np.isfinite(v)).sum()) for n, v in ch.items() if not np.isfinite(v).all()}
    if bad:
        findings = [f for f in findings if f.code != "signal_clean"]
        names = ", ".join(f"{data.labels.get(n, f'CH{n}')}: {c}" for n, c in sorted(bad.items()))
        findings.append(Finding(
            "bad_samples", "warn", "Capture contains invalid (NaN) samples",
            f"Non-finite samples: {names}. Statistics skip them, but a bad download can hide "
            f"real problems; re-capture before trusting a clean result.", {"counts": bad}))
    return Analysis(reports, supply, cmp, corr, findings, roles, basic)
