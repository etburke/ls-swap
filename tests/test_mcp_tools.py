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


from crank.sim import generate


def test_every_read_tool_copes_with_a_brand_new_empty_session(ctrl, tools):
    ctrl.sync(session_name="fresh")
    summary = tools.get_case_summary()
    assert summary["empty"] is True and summary["timeline"] == []
    assert tools.list_captures() == {"session": "fresh", "captures": []}
    assert "Nothing has been recorded" in tools.get_case_report()["text"]
    json.dumps(summary, allow_nan=False)


def test_get_case_summary_returns_the_timeline_and_suspects(ctrl, tools):
    make_capture(ctrl, label="baseline")
    ctrl.case().append("observation", "smells like fuel")
    ctrl.case().set_hypothesis(None, "Bad ground")
    s = tools.get_case_summary()
    assert s["empty"] is False
    assert any("baseline" in t["line"] for t in s["timeline"])
    assert [r["title"] for r in s["open"]] == ["Bad ground"]
    json.dumps(s, allow_nan=False)


def test_get_case_report_formats(ctrl, tools):
    make_capture(ctrl)
    md = tools.get_case_report()
    assert md["format"] == "markdown" and "Diagnostic summary" in md["text"]
    assert "<html" in tools.get_case_report(format="html")["text"]
    with pytest.raises(ToolError, match="markdown.*html"):
        tools.get_case_report(format="pdf")


def test_list_captures_gives_label_time_and_verdict_headline(ctrl, tools):
    make_capture(ctrl, "ref_sag", "baseline")
    caps = tools.list_captures()["captures"]
    assert [(c["id"], c["label"], c["sim"]) for c in caps] == [("001", "baseline", True)]
    assert caps[0]["verdict"].startswith("5V reference sags") and caps[0]["time"]


def test_get_capture_analysis_returns_ranked_findings_and_the_recorded_verdict(ctrl, tools):
    cid = make_capture(ctrl, "ref_sag")
    a = tools.get_capture_analysis(cid)
    json.dumps(a, allow_nan=False)
    assert a["findings"][0]["code"] == "ref_sag" and a["findings"][0]["severity"] == "fail"
    assert a["capture"]["id"] == cid and a["thresholds"]["ref_sag_mv"] == 300.0
    assert a["recorded_verdict"][0]["code"] == "ref_sag"
    assert a["signals"]["ch1"]["n_edges"] > 0 and a["supply"]["ref_sag_mv"] > 300


def test_get_capture_analysis_caps_a_long_event_list(ctrl, tools):
    cid = make_capture(ctrl, "ref_sag")                 # this scenario has hundreds of runt pulses
    a = tools.get_capture_analysis(cid)
    assert len(a["events"]) <= 200 and a["events_total"] >= len(a["events"])
    assert a["events_total"] > 200


def test_an_unknown_capture_lists_the_real_ones(ctrl, tools):
    make_capture(ctrl)
    with pytest.raises(ToolError, match="no capture '009'.*001"):
        tools.get_capture_analysis("009")
    with pytest.raises(ToolError, match="no capture '../x'"):
        tools.get_capture_signal("../x", 1)


def test_get_capture_signal_downsamples_but_keeps_the_extremes(ctrl, tools):
    cid = make_capture(ctrl, "ref_sag")
    data = ctrl.store.open("bench").load_capture(cid)
    sig = tools.get_capture_signal(cid, 3, max_points=500)
    json.dumps(sig, allow_nan=False)
    assert len(sig["t"]) == len(sig["v"]) <= 500 and sig["samples_in_window"] == len(data.t)
    assert min(sig["v"]) == pytest.approx(float(data.channels[3].min()), abs=1e-4)   # the deepest sag survives
    assert sig["label"] == "5V ref @ sensor" and sig["unit"] == "V"


def test_get_capture_signal_window_and_validation(ctrl, tools):
    cid = make_capture(ctrl)
    sig = tools.get_capture_signal(cid, 1, start=0.5, end=0.6, max_points=5000)
    assert sig["t"][0] >= 0.5 and sig["t"][-1] <= 0.6 and sig["returned"] == len(sig["t"])
    with pytest.raises(ToolError, match="Channel 9 was not captured.*1, 2, 3, 4"):
        tools.get_capture_signal(cid, 9)
    with pytest.raises(ToolError, match="max_points must be between 2 and 5000"):
        tools.get_capture_signal(cid, 1, max_points=10_000)
    with pytest.raises(ToolError, match="No samples between 50 and 60 s.*runs from"):
        tools.get_capture_signal(cid, 1, start=50, end=60)


def test_get_capture_signal_survives_invalid_samples_and_counts_them(ctrl, tools):
    d = generate("healthy", seconds=1.0)
    d.channels[1][1_000:30_000] = np.nan                # a stretch of failed samples
    cid = ctrl.store.open("bench").add_capture(d, "damaged")
    ctrl.sync(session_name="bench")
    sig = tools.get_capture_signal(cid, 1, max_points=400)
    json.dumps(sig, allow_nan=False)
    assert sig["nan_samples"] == 29_000
    assert tools.get_capture_analysis(cid)["findings"]    # the analysis still answers (bad_samples warning)


def test_compare_captures_shows_what_changed(ctrl, tools):
    a = make_capture(ctrl, "ref_sag", "before")
    b = make_capture(ctrl, "healthy", "after")
    c = tools.compare_captures(a, b)
    json.dumps(c, allow_nan=False)
    assert c["a"]["label"] == "before" and c["b"]["label"] == "after"
    assert "ref_sag" in c["differences"]["only_in_a"] and "signal_clean" in c["differences"]["only_in_b"]
    sag = c["differences"]["numbers"]["ref_sag_mv"]
    assert sag["a"] > 300 and sag["change"] == pytest.approx(sag["b"] - sag["a"]) and sag["change"] < 0


def test_compare_a_capture_with_itself_has_no_differences(ctrl, tools):
    a = make_capture(ctrl)
    d = tools.compare_captures(a, a)["differences"]
    assert d["only_in_a"] == d["only_in_b"] == [] and d["numbers"]["revs"]["change"] == 0


def test_compare_with_an_unknown_capture_is_refused(ctrl, tools):
    a = make_capture(ctrl)
    with pytest.raises(ToolError, match="no capture '009'"):
        tools.compare_captures(a, "009")


def test_reanalyze_applies_thresholds_without_saving_anything(ctrl, tools):
    cid = make_capture(ctrl, "ref_sag")
    sess = ctrl.store.open("bench")
    files_before = sorted(p.name for p in (sess.path / "captures").iterdir())
    events_before = len(ctrl.case().events())
    r = tools.reanalyze_capture(cid, {"ref_sag_mv": 5000})
    assert r["saved"] is False and r["thresholds"]["ref_sag_mv"] == 5000.0
    assert "ref_sag" not in [f["code"] for f in r["findings"]]
    assert ctrl.thresholds.ref_sag_mv == 300.0                       # the laptop's thresholds are untouched
    assert sorted(p.name for p in (sess.path / "captures").iterdir()) == files_before
    assert len(ctrl.case().events()) == events_before


def test_reanalyze_validates_thresholds(ctrl, tools):
    cid = make_capture(ctrl)
    with pytest.raises(ToolError, match="Unknown threshold 'bogus'.*ref_sag_mv"):
        tools.reanalyze_capture(cid, {"bogus": 1})
    with pytest.raises(ToolError, match="must be a finite number"):
        tools.reanalyze_capture(cid, {"hi": "high"})
    with pytest.raises(ToolError, match="must be a finite number"):
        tools.reanalyze_capture(cid, {"hi": float("nan")})
    teeth = tools.reanalyze_capture(cid, {"teeth": 58.0})["thresholds"]["teeth"]
    assert teeth == 58 and type(teeth) is int                        # integer fields stay integers
