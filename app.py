"""Crank signal test harness UI. Run with:  streamlit run app.py"""
import dataclasses
import time

import streamlit as st

from crank.analysis import analyze_capture
from crank.capture import CaptureJob
from crank.controller import get_controller, lan_ip
from crank.model import CaptureSettings, ChannelConfig, Thresholds
from crank.procedure import STEPS
from crank.report import build_figure, build_overlay, to_csv
from crank.scope import RigolScope, ScopeError
from crank.sim import SCENARIOS
import remote

PAGES = ["Capture", "Results", "Guided tests", "Compare"]
SEVERITY_ICON = {"fail": "🔴", "warn": "🟠", "info": "🟢"}

is_remote = st.query_params.get("page") == "remote"
st.set_page_config(page_title="Crank Remote" if is_remote else "Crank Test Harness",
                   layout="centered" if is_remote else "wide",
                   initial_sidebar_state="collapsed" if is_remote else "auto")
ctrl = get_controller()                               # shared by the laptop and phone sessions
if is_remote:
    remote.render(ctrl)
    st.stop()

ss = st.session_state
ss.setdefault("store", ctrl.store)
ss.setdefault("session_name", ctrl.session_name)
ss.setdefault("settings", ctrl.settings)
ss.setdefault("thresholds", ctrl.thresholds)
ss["job"] = ctrl.job                                  # a capture armed from the phone shows here too
ss.setdefault("view", None)           # capture id shown on Results
ss.setdefault("page", "Capture")


def goto(page, view=None):
    ss.page = page
    if view:
        ss.view = view


def session():
    return ss.store.open(ss.session_name)


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.title("Crank Test Harness")
    mode = st.radio("Scope", ["Simulator", "Real scope"], key="mode")
    job_busy = bool(ss.job and ss.job.running)
    if mode == "Simulator":
        st.selectbox("Scenario", SCENARIOS, key="scenario")
    else:
        st.text_input("VISA resource (blank = auto-detect)", key="resource")
        if st.button("Connect", key="connect", disabled=job_busy):
            try:
                if ctrl.scope:
                    ctrl.scope.close()
                ctrl.scope = RigolScope(ss.get("resource") or None)
                st.success(f"Connected: {ctrl.scope.identify()}")
            except ScopeError as e:
                ctrl.scope = None
                st.error(str(e))
        if ctrl.scope:
            st.caption("Connected to a real scope.")
            if st.button("Self-check settings", key="self_check", disabled=job_busy):
                try:
                    for ok, line in ctrl.scope.self_check(ss.settings):
                        (st.success if ok else st.error)(line)
                except ScopeError as e:
                    st.error(str(e))
                if ctrl.scope.log:
                    st.code("\n".join(ctrl.scope.log))

    st.divider()
    names = ss.store.list_sessions()
    if ss.session_name not in names:
        session()                                     # make sure the active session exists
        names = ss.store.list_sessions()
    st.selectbox("Session", names, index=names.index(ss.session_name), key="session_pick")
    ss.session_name = ss.session_pick
    new = st.text_input("New session name", key="new_session")
    if st.button("Create session", key="create_session") and new.strip():
        try:
            ss.session_name = ss.store.open(new).name
            st.rerun()
        except ValueError as e:
            st.error(str(e))
    st.divider()
    st.radio("Page", PAGES, key="page")
    st.caption("Phone remote (same Wi-Fi):")
    st.code(f"http://{lan_ip()}:{st.get_option('server.port')}/?page=remote", language=None)
    ctrl.sync(mode=mode, scenario=ss.get("scenario"), session_name=ss.session_name,
              settings=ss.settings, thresholds=ss.thresholds)


# ------------------------------------------------------------------ capture
def page_capture():
    st.header("Capture")
    s: CaptureSettings = ss.settings
    st.info("Wiring: CH1 crank at the PCM pin, CH2 crank at the sensor connector, CH3 5V reference, "
            "CH4 PCM low reference. All probe grounds to battery negative.")
    job: CaptureJob | None = ss.job
    busy = bool(job and job.running)

    with st.expander("Channels and settings", expanded=not busy):
        rows = st.data_editor(
            [dataclasses.asdict(c) for c in s.channels], key="chan_editor", hide_index=True,
            disabled=busy, column_config={"n": st.column_config.NumberColumn("CH", disabled=True)})
        c1, c2, c3, c4 = st.columns(4)
        seconds = c1.number_input("Capture length (s)", 1.0, 30.0, s.seconds, disabled=busy)
        pretrig = c2.number_input("Pre-trigger (fraction)", 0.0, 0.9, s.pretrig, disabled=busy)
        depth = c3.selectbox("Memory depth", ["1M", "10M", "25M"],
                             index=["1M", "10M", "25M"].index(s.depth), disabled=busy)
        trig = c4.number_input("Trigger level (V)", -10.0, 10.0, s.trig_level, disabled=busy)
        ss.settings = CaptureSettings(seconds, pretrig, depth, trig, s.wait,
                                      [ChannelConfig(**r) for r in rows])
        ctrl.sync(settings=ss.settings)

    label = st.text_input("Capture label (what changed?)", key="cap_label")
    note = st.text_input("Note", key="cap_note")

    if st.button("Arm", key="arm", type="primary", disabled=busy):
        err = ctrl.arm(label, note)
        if err:
            st.error(err)
            return
        ss.job = ctrl.job
        st.rerun()

    job = ss.job
    if job is None:
        return
    if job.running:
        if job.state in ("armed", "triggered"):
            st.warning(f"**ARMED: crank the engine now.**  Waiting {job.seconds_left:.0f} s more. "
                       f"State: {job.state}")
        else:
            st.info(f"State: {job.state}")
        if st.button("Cancel", key="cancel"):
            ctrl.cancel()
        time.sleep(0.5)
        st.rerun()
    elif job.state == "done":
        st.success("Capture complete and saved.")
        for line in getattr(job.scope, "log", []):
            if line.startswith("WARNING"):
                st.warning(line)
        st.button("View results", key="view_results", on_click=goto, args=("Results", job.saved_id))
    elif job.state in ("error", "cancelled"):
        (st.error if job.state == "error" else st.warning)(job.message)
    log = getattr(job.scope, "log", [])
    if log:
        with st.expander("Scope log (SCPI messages)", expanded=job.state == "error"):
            st.code("\n".join(log))


# ------------------------------------------------------------------ results
def show_verdict(a):
    order = {"fail": 0, "warn": 1, "info": 2}
    for f in sorted(a.findings, key=lambda f: order[f.severity]):
        with st.container(border=True):
            st.markdown(f"{SEVERITY_ICON[f.severity]} **{f.title}**")
            st.write(f.detail)


def summary_row(a):
    r = a.reports.get("ch1") or next(iter(a.reports.values()), None)
    s = a.supply
    return {
        "edges (CH1)": r.n_edges if r else None,
        "good revs": f"{r.revs_good}/{r.revs}" if r and r.usable else "n/a",
        "median RPM": round(r.rpm_median) if r and r.rpm_median else None,
        "runt highs": r.runt_hi if r else None,
        "missing-edge suspects": r.missing if r else None,
        "5V ref sag (mV)": round(s.get("ref_sag_mv", 0)),
        "ground dev (mV)": round(s.get("gnd_dev_mv", 0)),
    }


def thresholds_editor():
    with st.expander("Analysis thresholds"):
        vals = {}
        cols = st.columns(3)
        for i, f in enumerate(dataclasses.fields(Thresholds)):
            cur = getattr(ss.thresholds, f.name)
            kind = int if isinstance(cur, int) else float
            vals[f.name] = kind(cols[i % 3].number_input(f.name, value=cur, key=f"th_{f.name}"))
        ss.thresholds = Thresholds(**vals)


def page_results():
    st.header("Results")
    sess = session()
    caps = sess.list_captures()
    if not caps:
        st.info("No captures in this session yet. Use the Capture page.")
        return
    ids = [c["id"] for c in caps]
    default = ss.view if ss.view in ids else ids[-1]
    cid = st.selectbox("Capture", ids, index=ids.index(default), key="result_pick",
                       format_func=lambda i: f"{i}: {next(c['label'] for c in caps if c['id'] == i)}")
    thresholds_editor()
    data = sess.load_capture(cid)
    a = analyze_capture(data, ss.thresholds)
    show_verdict(a)
    st.dataframe([summary_row(a)], hide_index=True)

    t0, t1 = float(data.t[0]), float(data.t[-1])
    x0, x1 = st.slider("Time window (s)", t0, t1, (t0, t1), key="window")
    st.plotly_chart(build_figure(data, a, x0, x1), use_container_width=True)
    if a.events:
        with st.expander(f"Events ({len(a.events)})"):
            st.dataframe([{"t (s)": round(t, 5), "event": k} for t, k in a.events], hide_index=True)
    c1, c2 = st.columns(2)
    c1.download_button("Download .npz", open(sess.path / "captures" / f"{cid}.npz", "rb").read(),
                       file_name=f"{sess.name}_{cid}.npz")
    c2.download_button("Download CSV", to_csv(data), file_name=f"{sess.name}_{cid}.csv")


# ------------------------------------------------------------------ guided tests
def page_guided():
    st.header("Guided tests")
    sess = session()
    saved = sess.readings()
    for step in STEPS:
        done = step.id in saved
        with st.expander(f"{'✅ ' if done else ''}{step.title}", expanded=not done):
            st.markdown(f"**Do:** {step.instructions}")
            st.markdown(f"**Expect:** {step.expected}")
            vals = {}
            prev = saved.get(step.id, {})
            for f in step.fields:
                key = f"f_{step.id}_{f.key}"
                lab = f"{f.label} ({f.unit})" if f.unit else f.label
                if f.kind == "bool":
                    vals[f.key] = st.checkbox(lab, value=bool(prev.get(f.key, False)), key=key)
                elif f.kind == "text":
                    vals[f.key] = st.text_area(lab, value=prev.get(f.key, ""), key=key)
                else:
                    vals[f.key] = st.number_input(lab, value=float(prev.get(f.key, 0.0)),
                                                  format="%.3f", key=key)
            if step.fields and st.button("Save step", key=f"save_{step.id}"):
                sess.set_reading(step.id, vals)
                st.rerun()
            if step.kind == "capture":
                st.button("Go to Capture", key=f"go_{step.id}", on_click=goto, args=("Capture",))
    notes = st.text_area("Session notes", value=sess.notes(), key="session_notes")
    if st.button("Save notes", key="save_notes"):
        sess.set_notes(notes)
        st.success("Saved.")


# ------------------------------------------------------------------ compare
def page_compare():
    st.header("Compare")
    cols = st.columns(2)
    picks = []
    for side, col in zip("AB", cols):
        names = ss.store.list_sessions()
        sname = col.selectbox(f"Session {side}", names,
                              index=names.index(ss.session_name), key=f"cmp_sess_{side}")
        sess = ss.store.open(sname)
        caps = sess.list_captures()
        if not caps:
            col.info("No captures.")
            return
        cid = col.selectbox(f"Capture {side}", [c["id"] for c in caps], key=f"cmp_cap_{side}",
                            format_func=lambda i, caps=caps: f"{i}: {next(c['label'] for c in caps if c['id'] == i)}")
        data = sess.load_capture(cid)
        a = analyze_capture(data, ss.thresholds)
        picks.append((f"{side}: {sname}/{cid}", data, a))
        with col:
            show_verdict(a)
    st.dataframe([{"capture": n, **summary_row(a)} for n, _, a in picks], hide_index=True)
    chans = sorted(set(picks[0][1].channels) & set(picks[1][1].channels))
    if chans:
        ch = st.selectbox("Channel to overlay", chans, key="cmp_ch",
                          format_func=lambda n: picks[0][1].labels.get(n, f"CH{n}"))
        t0 = max(picks[0][1].t[0], picks[1][1].t[0])
        t1 = min(picks[0][1].t[-1], picks[1][1].t[-1])
        x0, x1 = st.slider("Time window (s)", float(t0), float(t1), (float(t0), float(t1)),
                           key="cmp_window")
        st.plotly_chart(build_overlay(picks[0][1], picks[1][1], ch, (picks[0][0], picks[1][0]), x0, x1),
                        use_container_width=True)


{"Capture": page_capture, "Results": page_results,
 "Guided tests": page_guided, "Compare": page_compare}[ss.page]()
