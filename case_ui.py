"""The Case page: vehicle profile, quick add, hypotheses board, timeline, hand-off report."""
import os
import time
from dataclasses import asdict, fields

import streamlit as st

from crank.controller import get_controller
from crank.obd.transport import SCENARIOS
from crank.case import EVENT_TYPES, HYPOTHESIS_STATUSES, Case, Vehicle, suggest_hypotheses
from crank.render import capture_svg, to_html, to_markdown
from crank.summary import build_summary

LABELS = {"year": "Year", "make": "Make", "model": "Model", "engine": "Engine",
          "transmission": "Transmission", "mileage": "Mileage", "modifications": "Modifications",
          "vin": "VIN (optional)", "problem": "Problem statement"}


def _vehicle(case):
    v = case.vehicle()
    with st.expander("Vehicle and problem", expanded=v.empty):
        vals = {}
        cols = st.columns(3)
        for i, f in enumerate(fields(Vehicle)):
            box = cols[i % 3] if f.name not in ("modifications", "problem") else st
            key = f"veh_{f.name}_{case.name}"
            if f.name in ("modifications", "problem"):
                vals[f.name] = box.text_area(LABELS[f.name], value=getattr(v, f.name), key=key)
            else:
                vals[f.name] = box.text_input(LABELS[f.name], value=getattr(v, f.name), key=key)
        if st.button("Save vehicle", key="save_vehicle"):
            case.set_vehicle(Vehicle(**vals))
            st.success("Saved.")


def _quick_add(case, capture_ids):
    ss = st.session_state
    ss.setdefault("qa_n", 0)
    n = ss.qa_n
    st.subheader("Log something")
    kind = st.selectbox("What kind of entry?", [t for t in EVENT_TYPES if t != "capture"], key="qa_type")
    text, data, refs = "", {}, {}
    if kind in ("symptom", "observation", "change"):
        text = st.text_area({"symptom": "What does it do wrong, and when?",
                             "observation": "What did you notice?",
                             "change": "What did you change, and what happened?"}[kind], key=f"qa_text_{n}")
        if capture_ids and st.checkbox("Attach to a capture", key=f"qa_attach_{n}"):
            refs["capture"] = st.selectbox("Capture", capture_ids, key=f"qa_cap_{n}")
        if kind == "change" and capture_ids:
            labels = {c["id"]: f"{c['id']}: {c.get('label') or 'unlabeled'}" for c in case.session.list_captures()}
            pick = ["infer from order"] + capture_ids
            for side in ("before", "after"):
                sel = st.selectbox(f"Capture {side} the change (optional)", pick, key=f"qa_{side}_{n}",
                                   format_func=lambda i: labels.get(i, i))
                if sel != pick[0]:
                    refs[side] = sel
    elif kind == "code_read":
        raw = st.text_input("Codes, comma separated (e.g. P0335, P0016)", key=f"qa_codes_{n}")
        status = st.selectbox("Status", ["current", "pending", "history"], key=f"qa_cstatus_{n}")
        data["codes"] = [{"code": c.strip().upper(), "description": "", "status": status}
                         for c in raw.split(",") if c.strip()]
        data["source"] = "manual"
    elif kind == "measurement":
        c1, c2, c3 = st.columns(3)
        data["name"] = c1.text_input("What was measured", key=f"qa_mname_{n}")
        data["value"] = c2.number_input("Value", value=None, format="%.3f", key=f"qa_mval_{n}")
        data["unit"] = c3.text_input("Unit", key=f"qa_munit_{n}")
        data["where"] = st.text_input("Where", key=f"qa_mwhere_{n}")
        data["conditions"] = st.text_input("Conditions (key off, cranking, hot)", key=f"qa_mcond_{n}")
    elif kind == "hypothesis":
        data["title"] = st.text_input("Suspect", key=f"qa_htitle_{n}")
    if st.button("Add entry", key="qa_add", type="primary"):
        try:
            if kind == "hypothesis":
                case.set_hypothesis(None, data["title"])
            else:
                case.append(kind, text, data, refs)
            ss.qa_n += 1
            st.rerun()
        except ValueError as e:
            st.error(str(e))


def pick_obd_source(key: str):
    """Simulator, real adapter or off. Returns a Controller.read_obd source, or None if off or no adapter port."""
    mode = st.radio("Source", ["Simulator", "Real adapter", "Off"], key=f"{key}_mode", horizontal=True)
    if mode == "Off":
        st.caption("OBD off.")
        return None
    if mode == "Simulator":
        return st.selectbox("Simulated vehicle", list(SCENARIOS), key=f"{key}_scenario")
    from crank.obd.serial_transport import list_ports
    try:
        ports = list_ports()
    except ImportError:
        st.error("Install pyserial (pip install -r requirements.txt) to use a real adapter.")
        return None
    st.button("Refresh ports", key=f"{key}_refresh")
    if not ports:
        st.warning("No adapter port found. Make sure the adapter is paired in Bluetooth settings, "
                   "then refresh.")
        return None
    choice = st.selectbox("Adapter port", ["Auto-detect"] + [label for label, _ in ports], key=f"{key}_port")
    if choice == "Auto-detect":
        return ("serial", "auto")
    return ("serial", dict(ports)[choice])


def obd_panel(ctrl, source, key: str, label: str, full_width: bool = False) -> None:
    """The start button, the running indicator with Stop and Cancel, and the last read's result.

    The result shows only on the session the read was started on, and stays until the next read starts."""
    job = ctrl.obd_job
    running = bool(job and job.running)
    if st.button(label, key=key, use_container_width=full_width, disabled=source is None or running):
        err = ctrl.start_obd(source)
        if err:
            st.error(err)
            return
        job = ctrl.obd_job
        job.join(0.5)                                 # a quick read finishes before the page is drawn
    if job is None or job.session_name != ctrl.session_name:
        return
    if job.running:
        st.info(f"Reading from the vehicle… {job.seconds:.0f} s")
        c1, c2 = st.columns(2)
        if c1.button("Stop (keep what was read)", key=f"{key}_stop", use_container_width=True):
            ctrl.stop_obd()
            job.join(2.0)
        if c2.button("Cancel", key=f"{key}_cancel", use_container_width=True):
            ctrl.cancel_obd()
            job.join(2.0)
        if job.running:
            refresh = float(os.environ.get("CRANK_REMOTE_REFRESH_S", "1"))
            if refresh > 0:                           # tests set 0 to render once instead of polling
                time.sleep(refresh)
                st.rerun()
            return
    if job.state in ("done", "stopped"):
        st.success("Recorded in the case:" if job.state == "done"
                   else "Stopped; kept what was read. Recorded in the case:")
        for line in job.readout.lines():
            st.write("- " + line)
    elif job.state == "cancelled":
        st.warning(job.message)
    elif job.state == "error":
        st.error(job.message)


WATCH_MODES = {"Raw CAN (default)": "raw", "Poll RPM": "poll"}


def _watch_results(ctrl, job) -> None:
    """The finished watch, shown as the Watches page shows it: case line, PID picker and chart (never claims more
    than best-effort for decoded CAN values)."""
    from watch_ui import render_watch
    if not job.meta:
        return
    render_watch(ctrl, job.session_name, job.meta["id"], f"case_watch_{job.meta['id']}")


def watch_panel(ctrl, key: str, compact: bool = False) -> None:
    """Start, Stop (keep) and Cancel for an OBD watch, its running indicator, and (not compact) its result."""
    job = ctrl.watch_job
    running = bool(job and job.running)
    status = ctrl.connection_status()
    reason = "" if status["state"] == "connected" else "Connect to the adapter first."
    mode = WATCH_MODES[st.selectbox("Watch mode", list(WATCH_MODES), key=f"{key}_mode", disabled=running)]
    seconds = st.number_input("Seconds", 1, 120, 20, key=f"{key}_seconds", disabled=running)
    if reason:
        st.caption(reason)
    st.caption("Raw CAN decodes are best-effort guesses; poll RPM (010C) is the official PID.")
    if st.button("Start watch", key=f"{key}_start", use_container_width=compact,
                 disabled=running or bool(reason)):
        err = ctrl.start_watch(mode, float(seconds))
        if err:
            st.error(err)
            return
        job = ctrl.watch_job
        job.join(0.3)
    if job is None or job.session_name != ctrl.session_name:
        return
    if job.running:
        if job.mode == "poll":
            count = f"{job.lines} lines"
            rpm = "" if job.latest_rpm is None else f" · latest RPM 010C (official) {job.latest_rpm:,.0f} rpm"
        else:
            count = f"{job.frames} frames"
            rpm = "" if job.latest_rpm is None else f" · latest engine speed {job.latest_rpm:,.0f} rpm (best-effort)"
        st.info(f"Watching… {job.seconds:.0f} s · {count}{rpm}")
        if job.linked:                               # monitoring starts before the scope is even configured
            st.info("Linked to the capture: crank when the capture says Armed")
        elif job.monitoring.is_set():
            st.warning("**Recording — crank now**")
        c1, c2 = st.columns(2)
        if c1.button("Stop (keep)", key=f"{key}_stop", use_container_width=True):
            ctrl.stop_watch()
            job.join(3.0)
        if c2.button("Cancel", key=f"{key}_cancel", use_container_width=True):
            ctrl.cancel_watch()
            job.join(3.0)
        if job.running:
            refresh = float(os.environ.get("CRANK_REMOTE_REFRESH_S", "1"))
            if refresh > 0:
                time.sleep(refresh)
                st.rerun()
            return
    if job.state in ("done", "stopped"):
        if compact:
            st.success("Watch recorded in the case.")
        else:
            _watch_results(ctrl, job)
    elif job.state == "cancelled":
        st.warning(job.message or "Watch cancelled; nothing was recorded.")
    elif job.state == "error":
        st.error(job.message)


def _obd():
    st.subheader("Read from the vehicle (OBD-II)")
    st.caption("A read adds the codes, freeze frame and live values to the case; it never clears codes. "
               "Real-adapter reads are recorded as real, not simulated.")
    ctrl = get_controller()
    if ctrl.connection_status()["state"] == "connected":
        st.caption("Using the open connection.")
        source = "connection"
    else:
        source = pick_obd_source("obd")
    obd_panel(ctrl, source, "obd_read", "Read from OBD")
    st.subheader("Watch the PCM (OBD-II)")
    watch_panel(ctrl, "obd_watch")


def _board(case, last_codes):
    st.subheader("Suspects")
    hyps = case.hypotheses()
    titles = [e.data.get("title", "") for e in hyps.values()]
    sugg = suggest_hypotheses(last_codes, titles)
    if sugg:
        st.caption("Suggested by the latest verdict:")
        for i, s in enumerate(sugg):
            if st.button(f"Add suspect: {s}", key=f"sug_{i}"):
                case.set_hypothesis(None, s)
                st.rerun()
    cols = st.columns(3)
    for col, status in zip(cols, HYPOTHESIS_STATUSES):
        col.markdown(f"**{status.replace('_', ' ').title()}**")
        for hid, e in hyps.items():
            if e.data.get("status", "open") != status:
                continue
            with col.container(border=True):
                st.write(e.data.get("title", ""))
                if e.data.get("reason"):
                    st.caption(e.data["reason"])
                new = st.selectbox("Move to", HYPOTHESIS_STATUSES, index=HYPOTHESIS_STATUSES.index(status),
                                   key=f"hs_{case.name}_{hid}_{e.id}")
                reason = st.text_input("Reason", key=f"hr_{case.name}_{hid}_{e.id}")
                if st.button("Update", key=f"hu_{case.name}_{hid}_{e.id}"):
                    try:
                        case.set_hypothesis(hid, e.data["title"], new, reason, e.refs)
                        st.rerun()
                    except ValueError as err:
                        st.error(str(err))


def _timeline(case, events):
    st.subheader("Timeline")
    types = sorted({e.type for e in events})
    keep = st.multiselect("Show", types, default=types, key="tl_filter")
    from crank.summary import describe
    for e in reversed(events):
        if e.type in keep:
            ai = "**[AI]** " if e.source else ""
            st.markdown(f"`{e.ts[:16].replace('T', ' ') or 'undated'}` {ai}{describe(e)}")


def _signature(case):
    """Changes whenever the event log or the vehicle profile changes."""
    return (len(case.all_events()), tuple(asdict(case.vehicle()).values()))


def _report(case):
    ss = st.session_state
    st.subheader("Hand-off report")
    if st.button("Generate report", key="gen_report"):
        s = build_summary(case)
        plots = {}
        for pair in s.before_after:
            for side in ("before", "after"):
                cid = pair[side]["id"]
                try:
                    plots[cid] = capture_svg(case.session.load_capture(cid))
                except Exception:
                    pass
        ss["report"] = (case.name, to_markdown(s), to_html(s, plots), s.not_tried, s.empty, _signature(case))
    rep = ss.get("report")
    if rep and rep[0] == case.name:
        _, md, html, not_tried, empty, sig = rep
        if sig != _signature(case):
            st.warning("The case has changed since this report was generated. It is out of date: "
                       "generate it again before sending it.")
        if empty:
            st.warning("Nothing has been recorded for this case yet, so the report is empty.")
        elif not_tried:
            st.info("Gaps the report will show as not tried: " + "; ".join(not_tried))
        st.code(md, language="markdown")
        c1, c2 = st.columns(2)
        c1.download_button("Download Markdown", md, file_name=f"{case.name}_report.md", key="dl_md")
        c2.download_button("Download HTML", html, file_name=f"{case.name}_report.html", key="dl_html")


def render(ss):
    case = Case(ss.store.open(ss.session_name))
    events = case.all_events()
    st.header("Case")
    _vehicle(case)
    cap_ids = [c["id"] for c in case.session.list_captures()]
    _quick_add(case, cap_ids)
    _obd()
    snaps = [e.data["snapshot"] for e in events if e.type == "capture" and e.data.get("snapshot")]
    codes = [v["code"] for v in snaps[-1]["verdict"]] if snaps else []
    _board(case, codes)
    _timeline(case, events)
    _report(case)
