"""The sidebar's "AI access" block: the MCP endpoint, how to add it to Claude Code, and the control toggle."""
import streamlit as st


def _set_ai_control(ctrl) -> None:
    ctrl.ai_control = bool(st.session_state["ai_control"])   # only when this tab's user flips it


def ai_panel(ctrl, status) -> None:
    if status.state == "running":
        st.caption("MCP server (this computer only):")
        st.code(f"claude mcp add --transport http ls-swap {status.url}", language=None)
    else:
        st.warning(f"The MCP server is not running: {status.message}")
    st.toggle("Allow AI control", value=ctrl.ai_control, key="ai_control", on_change=_set_ai_control, args=(ctrl,),
              help="Lets a connected AI assistant arm captures, read the vehicle and add notes to the case. "
                   "Off, it can still read and analyze everything. Resets to off when the app restarts.")
    if ctrl.ai_control:
        st.caption("AI control is on: anything it adds is marked [AI].")
