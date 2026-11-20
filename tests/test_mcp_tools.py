import json

import numpy as np
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from crank.controller import Controller
from crank.mcp_tools import MAX_LOG_LINES, OFF_MESSAGE, Tools, jsonable
from crank.store import SessionStore


@pytest.fixture
def ctrl(tmp_path):
    return Controller(SessionStore(tmp_path / "sessions"), poll_s=0.0, logs_dir=tmp_path / "logs")


@pytest.fixture
def tools(ctrl):
    return Tools(ctrl)


def make_capture(ctrl, scenario="ref_sag", label="baseline", session="bench"):
    """Run one simulated capture to completion and return its id."""
    ctrl.sync(mode="Simulator", scenario=scenario, session_name=session)
    assert ctrl.arm(label) is None
    ctrl.job.join(30)
    assert ctrl.job.state == "done", ctrl.job.message
    return ctrl.job.saved_id


def test_controller_starts_with_ai_control_off(ctrl):
    assert ctrl.ai_control is False and ctrl.mcp is None


def test_jsonable_converts_numpy_tuples_and_non_finite_numbers():
    out = jsonable({"a": np.float64(1.5), "b": (np.int64(2), np.bool_(True)), "c": np.array([1.0, np.nan]),
                    "d": float("inf"), "e": {"x": {3, 1}}})
    assert out == {"a": 1.5, "b": [2, True], "c": [1.0, None], "d": None, "e": {"x": [1, 3]}}
    json.dumps(out, allow_nan=False)


def test_gate_refuses_with_a_clear_message_until_control_is_on(ctrl, tools):
    with pytest.raises(ToolError, match="AI control is off"):
        tools._gate()
    ctrl.ai_control = True
    tools._gate()
    assert "AI control is off" in OFF_MESSAGE


def test_list_sessions_names_the_current_one(ctrl, tools):
    make_capture(ctrl, session="bench")
    assert tools.list_sessions() == {"current": "bench", "sessions": ["bench"]}


@pytest.mark.parametrize("bad", ["../etc", "", "nope"])
def test_a_session_argument_that_is_not_a_session_is_refused(ctrl, tools, bad):
    make_capture(ctrl, session="bench")
    with pytest.raises(ToolError, match="no session.*bench"):
        tools.get_obd_log(session=bad)
    assert ctrl.store.list_sessions() == ["bench"]          # nothing was created


def test_get_status_is_json_safe_and_describes_everything(ctrl, tools):
    ctrl.sync(mode="Simulator", scenario="healthy", session_name="bench")
    s = tools.get_status()
    json.dumps(s, allow_nan=False)
    assert (s["mode"], s["scenario"], s["session"], s["ai_control"]) == ("Simulator", "healthy", "bench", False)
    assert s["capture"]["state"] == "idle"
    assert s["obd"]["connection"]["state"] == "disconnected" and s["obd"]["read"]["state"] == "idle"


def test_progress_tools_report_idle_and_a_finished_capture(ctrl, tools):
    assert tools.get_capture_progress()["state"] == "idle"
    assert tools.obd_progress() == {"state": "idle", "running": False}
    cid = make_capture(ctrl)
    p = tools.get_capture_progress()
    assert p["state"] == "done" and p["saved_id"] == cid
    assert any(f["severity"] == "fail" for f in p["findings"])


def test_get_obd_log_tails_the_session_log(ctrl, tools):
    ctrl.sync(session_name="bench")
    assert tools.get_obd_log() == {"session": "bench", "lines": [], "note": "No OBD log for this session yet."}
    r, err = ctrl.read_obd("healthy")
    assert err is None
    out = tools.get_obd_log(lines=20)
    assert 0 < len(out["lines"]) <= 20 and any(">>" in l for l in out["lines"])


@pytest.mark.parametrize("n", [0, -1, MAX_LOG_LINES + 1])
def test_get_obd_log_rejects_silly_line_counts(tools, n):
    with pytest.raises(ToolError, match="lines must be between"):
        tools.get_obd_log(lines=n)
