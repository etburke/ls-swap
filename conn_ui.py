"""Connection status, the Connect / Disconnect / Relink buttons and the live log page."""
import streamlit as st

from crank.obd.trafficlog import tail_lines


def status_text(s: dict) -> str:
    state = s["state"]
    if state == "connecting":
        return "🟠 Connecting…"
    if state == "connected":
        volts = f", {s['voltage']:.1f} V" if s.get("voltage") is not None else ""
        heard = s.get("last_heard_age")
        sim = " (simulated)" if s.get("kind") == "simulated" else ""
        return (f"🟢 Connected{sim}, {s['adapter']}{volts}"
                + (f", last heard {heard:.0f} s ago" if heard is not None else ""))
    if state == "dropped":
        return f"🔴 Dropped: {s['message']}. Press Relink."
    if state == "error":
        return f"🔴 {s['message']}"
    return "⚪ Disconnected"


def vehicle_line(s: dict) -> str:
    if s["vehicle"] == "answered" and s.get("vehicle_age") is not None:
        return f"Vehicle: answered {s['vehicle_age']:.0f} s ago"
    if s["vehicle"] == "no answer":
        return "Vehicle: no answer (is the key on?)"
    return "Vehicle: not checked"


def connection_panel(ctrl, key: str) -> None:
    """Status, vehicle line, adapter picker and the three buttons. Refreshes itself every 2 s."""
    from case_ui import pick_obd_source

    @st.fragment(run_every=2)
    def panel():
        s = ctrl.connection_status()
        st.markdown(status_text(s))
        st.caption(vehicle_line(s))
        if s.get("traffic_error"):
            st.warning(f"The log could not be written: {s['traffic_error']}")
        busy = s["reading"]
        up = s["state"] == "connected"
        source = None if up else pick_obd_source(f"{key}_src")
        c1, c2, c3 = st.columns(3)
        if c1.button("Connect", key=f"{key}_connect", disabled=up or busy or source is None,
                     use_container_width=True):
            err = ctrl.connect(source)
            if err:
                st.error(err)
            st.rerun()
        if c2.button("Disconnect", key=f"{key}_disconnect",
                     disabled=(not up and s["state"] != "dropped") or busy, use_container_width=True):
            ctrl.disconnect()
            st.rerun()
        if c3.button("Relink", key=f"{key}_relink", disabled=busy or not s["source"], use_container_width=True):
            err = ctrl.relink()
            if err:
                st.error(err)
            st.rerun()
    panel()


def render_log(ctrl) -> None:
    st.header("Log")
    st.caption(f"Every adapter command and reply: {ctrl.traffic.path}")
    c1, c2 = st.columns(2)
    paused = c1.toggle("Pause", key="log_pause")
    hide = c2.toggle("Hide heartbeats", value=True, key="log_hide_hb")

    @st.fragment(run_every=None if paused else 1)
    def tail():
        got = tail_lines(ctrl.traffic.path, 200, hide_heartbeats=hide)
        st.code("\n".join(got) if got else "(no log lines yet)", language=None)
    tail()
