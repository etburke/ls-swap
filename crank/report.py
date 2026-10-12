"""Text report and Plotly figures built from a CaptureData + Analysis."""
from __future__ import annotations

import io

import numpy as np

from .analysis import Analysis
from .model import CaptureData

SEVERITY_ORDER = {"fail": 0, "warn": 1, "info": 2}


def text_report(a: Analysis) -> str:
    out = []
    for f in sorted(a.findings, key=lambda f: SEVERITY_ORDER[f.severity]):
        out.append(f"[{f.severity.upper()}] {f.title}\n    {f.detail}")
    out.append("")
    for r in a.reports.values():
        out.append(f"{r.label}: {r.n_edges} rising edges" + (
            f", {r.revs_good}/{r.revs} good revolutions, median {r.rpm_median:.0f} RPM, "
            f"{r.missing} missing-edge suspects, {r.runt_hi} runt highs, {r.runt_lo} weak lows"
            if r.usable and r.rpm_median is not None else
            " (no usable signal)" if not r.usable else ""))
    s = a.supply
    if "ref_p1" in s:
        out.append(f"5V ref: 1st percentile {s['ref_p1']:.2f} V (sag {s['ref_sag_mv']:.0f} mV)")
    if "gnd_p99" in s:
        out.append(f"PCM low ref vs battery negative: 99th percentile |V| {s['gnd_p99']:.3f} V")
    if a.compare:
        c = a.compare
        out.append(f"PCM pin vs sensor connector: {c['n1']} vs {c['n2']} edges, "
                   f"delay median {c['delay_median_us']:.1f} us, worst {c['delay_worst_us']:.1f} us")
    return "\n".join(out)


def to_csv(data: CaptureData) -> bytes:
    cols = sorted(data.channels)
    arr = np.column_stack([data.t] + [data.channels[n] for n in cols])
    buf = io.StringIO()
    np.savetxt(buf, arr, delimiter=",", fmt="%.6g",
               header=",".join(["t_s"] + [data.labels.get(n, f"ch{n}") for n in cols]), comments="")
    return buf.getvalue().encode()


def decimate(t: np.ndarray, v: np.ndarray, max_points: int = 20000):
    """Min/max envelope decimation so runts and glitches survive downsampling."""
    n = len(v)
    if n <= max_points:
        return t, v
    bins = max_points // 2
    size = n // bins
    vv, tt = v[:bins * size].reshape(bins, size), t[:bins * size].reshape(bins, size)
    imin, imax = np.nanargmin(vv, axis=1), np.nanargmax(vv, axis=1)
    rows = np.arange(bins)
    i0, i1 = np.minimum(imin, imax), np.maximum(imin, imax)
    return (np.stack([tt[rows, i0], tt[rows, i1]], axis=1).ravel(),
            np.stack([vv[rows, i0], vv[rows, i1]], axis=1).ravel())


def build_figure(data: CaptureData, a: Analysis, x0: float | None = None,
                 x1: float | None = None, max_events: int = 300):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    t = data.t
    x0 = t[0] if x0 is None else x0
    x1 = t[-1] if x1 is None else x1
    m = (t >= x0) & (t <= x1)
    rows = [(data.labels.get(n, f"CH{n}"), data.channels[n]) for n in sorted(data.channels)]
    if 1 in data.channels and 4 in data.channels:
        rows.append(("Crank as PCM sees it (CH1-CH4)", data.channels[1] - data.channels[4]))
    fig = make_subplots(rows=len(rows), cols=1, shared_xaxes=True, vertical_spacing=0.03,
                        subplot_titles=[r[0] for r in rows])
    ev = [e[0] for e in a.events if x0 <= e[0] <= x1][:max_events]
    for k, (name, v) in enumerate(rows, start=1):
        td, vd = decimate(t[m], v[m])
        fig.add_trace(go.Scatter(x=td, y=vd, mode="lines", name=name, line=dict(width=1),
                                 showlegend=False), row=k, col=1)
        if ev and len(vd):
            lo, hi = float(np.nanmin(vd)), float(np.nanmax(vd))
            xs = np.repeat(ev, 3).astype(float)
            xs[2::3] = np.nan
            ys = np.tile([lo, hi, np.nan], len(ev))
            fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", hoverinfo="skip", showlegend=False,
                                     line=dict(color="rgba(220,0,0,0.35)", width=1)), row=k, col=1)
    fig.update_xaxes(title_text="s from trigger", row=len(rows), col=1)
    fig.update_layout(height=220 * len(rows) + 60, margin=dict(l=40, r=10, t=40, b=40))
    return fig


def build_overlay(a: CaptureData, b: CaptureData, ch: int, names=("A", "B"),
                  x0: float | None = None, x1: float | None = None):
    import plotly.graph_objects as go
    fig = go.Figure()
    for d, nm in ((a, names[0]), (b, names[1])):
        if ch not in d.channels:
            continue
        lo = d.t[0] if x0 is None else x0
        hi = d.t[-1] if x1 is None else x1
        m = (d.t >= lo) & (d.t <= hi)
        td, vd = decimate(d.t[m], d.channels[ch][m])
        fig.add_trace(go.Scatter(x=td, y=vd, mode="lines", name=nm, line=dict(width=1)))
    fig.update_layout(height=320, margin=dict(l=40, r=10, t=20, b=40), xaxis_title="s from trigger")
    return fig
