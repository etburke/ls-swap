import asyncio
import socket
import time

import pytest
from mcp import Client

from crank.controller import Controller, get_controller, reset_controller
from crank.mcp_server import TOOL_NAMES, build_server, ensure_mcp_server, stop_mcp_server
from crank.store import SessionStore

EXPECTED_TOOLS = {
    "list_sessions", "get_status", "get_capture_progress", "obd_progress", "get_obd_log", "get_case_summary",
    "get_case_report", "list_captures", "get_capture_analysis", "get_capture_signal", "compare_captures",
    "reanalyze_capture", "arm_capture", "cancel_capture", "obd_connect", "obd_disconnect", "obd_relink",
    "obd_read", "obd_stop", "obd_cancel", "add_case_note", "set_suspect",
}


@pytest.fixture
def ctrl(tmp_path):
    c = Controller(SessionStore(tmp_path / "sessions"), poll_s=0.0, logs_dir=tmp_path / "logs")
    yield c
    stop_mcp_server(c)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _call(c, name, args=None):
    r = await c.call_tool(name, args or {})
    return r


async def _until_done(c, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        p = (await c.call_tool("get_capture_progress", {})).structured_content
        if p["state"] in ("done", "error", "cancelled"):
            return p
        await asyncio.sleep(0.2)
    raise AssertionError("the capture did not finish")


def test_every_tool_is_registered_and_described(ctrl):
    assert set(TOOL_NAMES) == EXPECTED_TOOLS

    async def go():
        async with Client(build_server(ctrl)) as c:
            return (await c.list_tools()).tools
    tools = asyncio.run(go())
    assert {t.name for t in tools} == EXPECTED_TOOLS
    assert all(t.description for t in tools)


def test_gated_tools_report_why_over_the_protocol(ctrl):
    async def go():
        async with Client(build_server(ctrl)) as c:
            return await _call(c, "arm_capture", {"label": "x"})
    r = asyncio.run(go())
    assert r.is_error and "AI control is off" in r.content[0].text
    assert ctrl.job is None


def test_a_simulator_session_end_to_end_through_a_client(ctrl):
    ctrl.ai_control = True
    ctrl.sync(mode="Simulator", scenario="ref_sag", session_name="bench")

    async def go():
        async with Client(build_server(ctrl)) as c:
            assert not (await _call(c, "arm_capture", {"label": "before"})).is_error
            assert (await _until_done(c))["saved_id"] == "001"
            ctrl.sync(scenario="healthy")
            assert not (await _call(c, "arm_capture", {"label": "after"})).is_error
            assert (await _until_done(c))["saved_id"] == "002"
            analysis = (await _call(c, "get_capture_analysis", {"capture_id": "001"})).structured_content
            diff = (await _call(c, "compare_captures",
                                {"capture_a": "001", "capture_b": "002"})).structured_content
            note = await _call(c, "add_case_note", {"type": "observation", "text": "reground made it clean"})
            summary = (await _call(c, "get_case_summary")).structured_content
            return analysis, diff, note, summary
    analysis, diff, note, summary = asyncio.run(go())
    assert analysis["findings"][0]["code"] == "ref_sag"
    assert "ref_sag" in diff["differences"]["only_in_a"] and "signal_clean" in diff["differences"]["only_in_b"]
    assert not note.is_error
    assert [t["source"] for t in summary["timeline"]][-1] == "mcp"
    assert [c["label"] for c in summary["captures"]] == ["[AI] before", "[AI] after"]


def test_the_server_runs_once_on_localhost_and_a_real_client_connects(ctrl):
    port = free_port()
    status = ensure_mcp_server(ctrl, port)
    assert status.state == "running" and status.url == f"http://127.0.0.1:{port}/mcp"
    assert ensure_mcp_server(ctrl, port) is status              # a Streamlit rerun starts nothing new
    assert ctrl.mcp.status is status

    async def go():
        async with Client(status.url) as c:
            return {t.name for t in (await c.list_tools()).tools}
    assert asyncio.run(go()) == EXPECTED_TOOLS
    assert ctrl.mcp.server.config.host == "127.0.0.1"           # localhost only, never the LAN address

    stop_mcp_server(ctrl)
    assert ctrl.mcp is None
    with socket.socket() as s:
        assert s.connect_ex(("127.0.0.1", port)) != 0           # the port is closed again


def test_a_busy_port_degrades_to_a_status_instead_of_raising(ctrl):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        status = ensure_mcp_server(ctrl, port)
    assert status.state == "port_busy" and str(port) in status.message and "CRANK_MCP_PORT" in status.message
    assert ctrl.mcp.server is None and ctrl.mcp.thread is None


def test_port_zero_picks_a_free_port(ctrl):
    status = ensure_mcp_server(ctrl, 0)
    assert status.state == "running" and not status.url.endswith(":0/mcp")


def test_reset_controller_stops_the_server(tmp_path, monkeypatch):
    monkeypatch.setenv("CRANK_SESSIONS_DIR", str(tmp_path))
    monkeypatch.setenv("CRANK_LOGS_DIR", str(tmp_path / "logs"))
    reset_controller()
    ctrl = get_controller()
    status = ensure_mcp_server(ctrl, 0)
    port = int(status.url.split(":")[2].split("/")[0])
    reset_controller()
    with socket.socket() as s:
        assert s.connect_ex(("127.0.0.1", port)) != 0
