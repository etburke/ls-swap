"""The Watches page: reopen a saved OBD watch and chart any of its PIDs, with the linked capture's crank RPM."""
from __future__ import annotations

import streamlit as st

from crank.obd.pids import PIDS
from crank.obd.series import RAW_SOURCE, watch_pids
from crank.watch_plot import build_watch_figure


def _label(pid: int) -> str:
    p = PIDS[pid]
    return f"{pid:02X} {p.name} ({p.unit})"


def render_watch(ctrl, session_name: str, watch_id: str, key: str) -> None:
    """One saved watch: its case line, a PID picker (RPM by default), the chart, its source and notes."""
    import pandas as pd
    from crank.summary import describe
    ses = ctrl.store.open(session_name)
    meta, rows = ses.load_watch(watch_id)
    if session_name == ctrl.session_name:
        for e in ctrl.case().events():
            if e.type == "watch" and e.refs.get("watch") == watch_id:
                st.success(describe(e))
                break
    pids = watch_pids(meta, rows)
    if pids:
        options = [_label(p) for p in pids]
        chosen = st.multiselect("PIDs to plot", options, default=[options[0]], key=f"{key}_pids")
        capture = None
        if meta.get("capture_id"):
            try:
                capture = ses.load_capture(meta["capture_id"])
            except Exception:                             # a missing or unreadable capture: chart without it
                capture = None
        fig = build_watch_figure(meta, rows, [pids[options.index(c)] for c in chosen], capture, ctrl.thresholds)
        st.plotly_chart(fig, use_container_width=True, key=f"{key}_chart")
        source = ("Engine speed is a best-effort decode of raw CAN frames." if meta.get("mode") != "poll"
                  else "Values are official OBD answers (mode 01), polled while recording.")
        if capture is not None:
            source += f" Dotted line: per-revolution crank RPM from capture {meta['capture_id']} (scope CH1)."
        st.caption(source)
    else:
        st.caption("No engine speed or PID values were seen.")
    for note in meta.get("notes") or []:
        st.caption(note)
    ids = (meta.get("summary") or {}).get("ids") or {}
    if ids:
        st.caption("CAN IDs seen")
        st.dataframe(pd.DataFrame([{"ID": i, "Frames": v["count"], "Hz": v["hz"]} for i, v in ids.items()]),
                     hide_index=True)


def page_watches(ctrl) -> None:
    st.header("Watches")
    metas = list(reversed(ctrl.store.open(ctrl.session_name).list_watches()))
    if not metas:
        st.info("No OBD watches in this session yet. Start one from the Case page, or link one to a capture.")
        return
    by_id = {m["id"]: m for m in metas}

    def name(wid: str) -> str:
        m = by_id[wid]
        mode = "poll" if m.get("mode") == "poll" else "raw CAN"
        bits = [f"Watch {wid}", mode, f"{m.get('seconds', 0):.1f} s"]
        if m.get("capture_id"):
            bits.append(f"capture {m['capture_id']}")
        if m.get("label"):
            bits.append(m["label"])
        return " · ".join(bits)

    wid = st.selectbox("Watch", list(by_id), format_func=name, key="watch_pick")
    render_watch(ctrl, ctrl.session_name, wid, "watch_page")
