"""The chart for a saved OBD watch: chosen PIDs over time, one subplot per unit, the linked capture's crank RPM."""
from __future__ import annotations

from .analysis import analyze_capture
from .model import CaptureData, Thresholds
from .obd.pids import PIDS
from .obd.series import pid_series

SCOPE_RPM = "Scope CH1 (per rev)"


def _scope_rpm(meta: dict, capture: CaptureData | None, th: Thresholds | None) -> list[tuple[float, float]]:
    """The capture's per-revolution crank RPM on the watch's clock, or [] when it cannot be placed or has none."""
    if capture is None or 1 not in capture.channels or not meta.get("trigger_t") or not meta.get("started"):
        return []
    rep = analyze_capture(capture, th).reports.get("ch1")
    if rep is None or not rep.rev_rpm:
        return []
    shift = meta["trigger_t"] - meta["started"]
    return [(t + shift, r) for t, r in rep.rev_rpm]


def build_watch_figure(meta: dict, rows: list[dict], pids: list[int], capture: CaptureData | None = None,
                       th: Thresholds | None = None):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    if not pids:
        fig = go.Figure()
        fig.add_annotation(text="Pick a PID to plot", showarrow=False, x=0.5, y=0.5, xref="paper", yref="paper")
        fig.update_layout(height=200, xaxis=dict(visible=False), yaxis=dict(visible=False))
        return fig
    units: list[str] = []
    for p in pids:
        if PIDS[p].unit not in units:
            units.append(PIDS[p].unit)
    fig = make_subplots(rows=len(units), cols=1, shared_xaxes=True, vertical_spacing=0.06,
                        subplot_titles=[u for u in units])
    for p in pids:
        s = pid_series(meta, rows, p)
        row = units.index(s.unit) + 1
        fig.add_trace(go.Scatter(x=[a for a, _ in s.points], y=[b for _, b in s.points], mode="lines",
                                 name=s.name, line=dict(width=2)), row=row, col=1)
        fig.update_yaxes(title_text=s.unit, row=row, col=1)
    if 0x0C in pids:
        scope = _scope_rpm(meta, capture, th)
        if scope:
            fig.add_trace(go.Scatter(x=[a for a, _ in scope], y=[b for _, b in scope], mode="lines+markers",
                                     name=SCOPE_RPM, line=dict(width=2, dash="dot"), marker=dict(size=8)),
                          row=units.index(PIDS[0x0C].unit) + 1, col=1)
    if meta.get("trigger_t") and meta.get("started"):
        fig.add_vline(x=meta["trigger_t"] - meta["started"], line=dict(width=1, dash="dash", color="gray"),
                      annotation_text="Scope trigger", annotation_position="top")
    fig.update_xaxes(title_text="Seconds from start of recording", row=len(units), col=1)
    fig.update_layout(height=240 * len(units) + 80, margin=dict(l=50, r=10, t=50, b=40),
                      legend=dict(orientation="h", y=1.08))
    return fig
