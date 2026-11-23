"""The MCP endpoint: tool registration and a localhost-only server thread inside the app process."""
from __future__ import annotations

import os
import socket
import threading
import time
from dataclasses import dataclass

import uvicorn
from mcp.server.mcpserver import MCPServer

from .mcp_tools import Tools

DEFAULT_PORT = 8765
HOST = "127.0.0.1"

TOOL_NAMES = (
    "list_sessions", "get_status", "get_capture_progress", "obd_progress", "get_obd_log",
    "get_case_summary", "get_case_report", "list_captures", "get_capture_analysis", "get_capture_signal",
    "compare_captures", "reanalyze_capture", "watch_progress", "list_watches", "get_watch",
    "arm_capture", "cancel_capture", "obd_connect", "obd_disconnect", "obd_relink", "obd_read", "obd_stop",
    "obd_cancel", "obd_watch", "obd_watch_stop", "obd_watch_cancel", "add_case_note", "set_suspect",
    "set_capture_roles",
)

INSTRUCTIONS = """\
Tools for a bench harness that diagnoses a 0 RPM while cranking problem on an LS-swapped truck by recording a
crank signal, its 5V reference and the PCM ground on an oscilloscope, and by reading the PCM over OBD-II.
A verdict is what the harness reported, not a diagnosis: say which area the evidence points to and how
strong it is, and say "insufficient data" when it is. Simulated data is flagged "sim": never treat it as
evidence about the vehicle. Read tools always work. arm_capture, the obd_* control tools, add_case_note,
set_suspect and set_capture_roles only work while the owner has switched on "Allow AI control". Each capture
records what every channel was connected to (its roles); the analysis follows them, and a capture analysed
with the wrong roles can be corrected with set_capture_roles. Arming only arms: in real-scope mode
tell the owner to crank the engine. Everything you add to the case is marked as AI-authored; only record what
was actually observed or done.
OBD watch values decoded from raw CAN frames are best-effort (community-documented layouts) and are labelled so; treat them as what the PCM broadcast, not as verified fact. Polled 010C RPM is official OBD data."""


@dataclass
class McpStatus:
    state: str              # "running", "port_busy" or "error"
    url: str = ""
    message: str = ""


class McpHandle:
    """What the controller keeps for its server: the status, and the thread and uvicorn server to stop."""

    def __init__(self, status: McpStatus, server=None, thread=None):
        self.status, self.server, self.thread = status, server, thread


def build_server(ctrl) -> MCPServer:
    tools = Tools(ctrl)
    server = MCPServer("ls-swap crank harness", instructions=INSTRUCTIONS)
    for name in TOOL_NAMES:
        server.tool(name=name)(getattr(tools, name))
    return server


def _free(port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)      # as uvicorn binds: TIME_WAIT is not "busy"
        try:
            s.bind((HOST, port))
        except OSError:
            return False
    return True


def _pick(port: int) -> int:
    if port:
        return port
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


_lock = threading.Lock()


def ensure_mcp_server(ctrl, port: int | None = None) -> McpStatus:
    """Start this controller's MCP server once and return its status. Calling it again changes nothing, so a
    Streamlit rerun is harmless. A busy port or a failed start is a status, never an exception."""
    with _lock:
        if ctrl.mcp is not None:
            return ctrl.mcp.status
        raw = os.environ.get("CRANK_MCP_PORT", str(DEFAULT_PORT)) if port is None else port
        try:
            port = int(raw)
            if not 0 <= port <= 65535:
                raise ValueError(raw)
            port = _pick(port)
        except (ValueError, OSError):
            ctrl.mcp = McpHandle(McpStatus("error", "", f"CRANK_MCP_PORT must be a port number from 0 to 65535 "
                                                        f"(got {raw!r})."))
            return ctrl.mcp.status
        url = f"http://{HOST}:{port}/mcp"
        if not _free(port):
            ctrl.mcp = McpHandle(McpStatus("port_busy", url, f"Port {port} is in use. Set CRANK_MCP_PORT to "
                                                              f"another port and restart the app."))
            return ctrl.mcp.status
        try:
            app = build_server(ctrl).streamable_http_app(host=HOST)
            server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=port, log_level="warning"))
            thread = threading.Thread(target=server.run, daemon=True, name="mcp-server")
            thread.start()
            end = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < end:
                time.sleep(0.05)
            if not server.started:
                raise RuntimeError("the server did not start")
        except Exception as e:
            ctrl.mcp = McpHandle(McpStatus("error", url, f"{type(e).__name__}: {e}"))
            return ctrl.mcp.status
        ctrl.mcp = McpHandle(McpStatus("running", url), server, thread)
        return ctrl.mcp.status


def stop_mcp_server(ctrl) -> None:
    """Stop this controller's server (used by tests and when the controller is reset)."""
    with _lock:
        handle, ctrl.mcp = ctrl.mcp, None
    if handle and handle.server is not None:
        handle.server.should_exit = True
        handle.thread.join(5)
