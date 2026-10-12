"""Phone remote page (open the app with ?page=remote). Drives the same capture as the laptop."""
import os
import time

import streamlit as st

ICON = {"fail": "🔴", "warn": "🟠", "info": "🟢"}
BIG_BUTTONS = """<style>
div.stButton > button { height: 5rem; font-size: 1.6rem; }
</style>"""


def render(ctrl):
    st.markdown(BIG_BUTTONS, unsafe_allow_html=True)
    st.title("Crank remote")
    snap = ctrl.snapshot()
    what = snap["scenario"] if snap["mode"] == "Simulator" else "real scope"
    st.caption(f"{snap['mode']}: {what} · session {snap['session_name']}")

    label = st.text_input("What changed? (label)", key="remote_label")
    if st.button("ARM", key="remote_arm", type="primary", use_container_width=True,
                 disabled=snap["running"]):
        err = ctrl.arm(label)
        if err:
            st.error(err)
        else:
            st.rerun()

    with st.expander("Log something"):
        note = st.text_input("What happened?", key="remote_note")
        c1, c2 = st.columns(2)
        for col, kind, key in ((c1, "observation", "remote_obs"), (c2, "change", "remote_change")):
            if col.button(f"Add {kind}", key=key, use_container_width=True):
                try:
                    ctrl.case().append(kind, note)
                    st.success(f"Added {kind}.")
                except ValueError as e:
                    st.error(str(e))

    from crank.obd.transport import SCENARIOS
    with st.expander("Read OBD (simulated)"):
        scen = st.selectbox("Simulated vehicle", list(SCENARIOS), key="remote_obd_scenario")
        if st.button("Read OBD", key="remote_obd", use_container_width=True):
            readout, err = ctrl.read_obd(scen)
            if err:
                st.error(err)
            else:
                st.success("Recorded in the case:")
                for line in readout.lines():
                    st.write("- " + line)

    state = snap["state"]
    if snap["running"]:
        if state in ("armed", "triggered"):
            st.warning(f"**ARMED: crank the engine now.**\n\n{state}, "
                       f"{snap['seconds_left']:.0f} s left to trigger")
        else:
            st.info(f"{state}...")
        if st.button("Cancel", key="remote_cancel", use_container_width=True):
            ctrl.cancel()
        refresh = float(os.environ.get("CRANK_REMOTE_REFRESH_S", "1"))
        if refresh > 0:                       # tests set 0 to render once instead of polling
            time.sleep(refresh)
            st.rerun()
    elif state == "done":
        st.success(f"Saved as capture {snap['saved_id']}.")
        for w in snap["warnings"]:
            st.warning(w)
        for f in snap["findings"]:
            st.markdown(f"{ICON[f['severity']]} **{f['title']}**")
        st.caption("Full plots are on the laptop's Results page.")
    elif state in ("error", "cancelled"):
        (st.error if state == "error" else st.warning)(snap["message"])
    if state in ("done", "error") and snap["hook_error"]:
        st.warning(f"The capture was saved but could not be added to the case: {snap['hook_error']}")
