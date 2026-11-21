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
    assert tools.list_watches() == {"session": "fresh", "watches": []}
    assert tools.watch_progress()["state"] == "idle"
    with pytest.raises(ToolError, match="Watches: none"):
        tools.get_watch("001")


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


GATED_CONTROL = [
    lambda t: t.arm_capture(),
    lambda t: t.cancel_capture(),
    lambda t: t.obd_connect("sim:healthy"),
    lambda t: t.obd_disconnect(),
    lambda t: t.obd_relink(),
    lambda t: t.obd_read("sim:healthy"),
    lambda t: t.obd_stop(),
    lambda t: t.obd_cancel(),
    lambda t: t.obd_watch(),
    lambda t: t.obd_watch_stop(),
    lambda t: t.obd_watch_cancel(),
]


@pytest.mark.parametrize("call", GATED_CONTROL)
def test_control_tools_refuse_while_ai_control_is_off(ctrl, tools, call):
    with pytest.raises(ToolError, match="AI control is off"):
        call(tools)
    assert ctrl.job is None and ctrl.obd_job is None and ctrl.connection.state == "disconnected"


def test_arm_capture_labels_the_capture_as_ai_and_the_laptop_sees_it(ctrl, tools):
    ctrl.ai_control = True
    ctrl.sync(mode="Simulator", scenario="ref_sag", session_name="bench")
    r = tools.arm_capture("after reground", "note")
    assert r["armed"] is True and r["label"] == "[AI] after reground" and "simulator" in r["message"].lower()
    ctrl.job.join(30)
    p = tools.get_capture_progress()
    assert p["state"] == "done" and p["saved_id"] == "001"
    assert tools.list_captures()["captures"][0]["label"] == "[AI] after reground"
    assert ctrl.case().events()[-1].text == "[AI] after reground"
    assert tools.arm_capture()["label"] == "[AI] capture"             # no label given
    ctrl.job.join(30)


def test_arm_capture_in_real_mode_without_a_scope_says_so(ctrl, tools):
    ctrl.ai_control = True
    ctrl.sync(mode="Real scope")
    with pytest.raises(ToolError, match="Connect to the scope"):
        tools.arm_capture()
    assert ctrl.job is None


def test_arm_while_a_capture_is_running_is_refused_and_cancel_works(ctrl, tools):
    ctrl.ai_control = True
    ctrl.sim_fault = "stuck_waiting"
    ctrl.sync(mode="Simulator", scenario="healthy", session_name="bench")
    tools.arm_capture()
    try:
        with pytest.raises(ToolError, match="already running"):
            tools.arm_capture()
        assert tools.cancel_capture()["cancelled"] is True
    finally:
        ctrl.cancel()
        ctrl.job.join(5)
    assert tools.get_capture_progress()["state"] == "cancelled"
    assert tools.cancel_capture() == {"cancelled": False, "message": "No capture is running."}


def test_turning_control_off_mid_capture_blocks_new_calls_but_not_progress(ctrl, tools):
    ctrl.ai_control = True
    ctrl.sim_fault = "stuck_waiting"
    ctrl.sync(mode="Simulator", scenario="healthy", session_name="bench")
    tools.arm_capture()
    ctrl.ai_control = False
    try:
        assert tools.get_capture_progress()["running"] is True        # still readable
        assert tools.get_status()["ai_control"] is False
        with pytest.raises(ToolError, match="AI control is off"):
            tools.cancel_capture()
        assert ctrl.running                                           # and the capture was not cancelled
    finally:
        ctrl.cancel()
        ctrl.job.join(5)


def test_obd_connect_read_and_disconnect_on_the_simulator(ctrl, tools):
    ctrl.ai_control = True
    ctrl.sync(session_name="bench")
    assert tools.obd_connect("sim:healthy")["state"] == "connected"
    try:
        assert tools.obd_read()["started"] is True                    # default source: the open connection
        ctrl.obd_job.join(30)
        p = tools.obd_progress()
        assert p["state"] == "done" and "get_case_summary" in p["note"]
        assert [e.type for e in ctrl.case().events()].count("code_read") == 1
    finally:
        assert tools.obd_disconnect()["state"] == "disconnected"


def test_obd_read_without_a_connection_uses_its_own_port(ctrl, tools):
    ctrl.ai_control = True
    ctrl.sync(session_name="bench")
    tools.obd_read("sim:healthy")
    ctrl.obd_job.join(30)
    assert tools.obd_progress()["state"] == "done"
    with pytest.raises(ToolError, match="connection is closed"):
        tools.obd_read()                                              # 'connection' but nothing is connected


@pytest.mark.parametrize("source, match", [
    ("bogus", "source must be"), ("sim:", "source must be"), ("serial:", "source must be"),
    ("sim:no_such_scenario", "Unknown simulator scenario"), ("connection", "obd_connect needs"),
])
def test_obd_sources_are_validated(ctrl, tools, source, match):
    ctrl.ai_control = True
    with pytest.raises(ToolError, match=match):
        tools.obd_connect(source)
    assert ctrl.connection.state == "disconnected"


def test_obd_cancel_throws_a_slow_read_away_and_records_nothing(ctrl, tools):
    ctrl.ai_control = True
    ctrl.sync(session_name="bench")
    tools.obd_read("sim:slow_adapter")
    try:
        with pytest.raises(ToolError, match="already running"):
            tools.obd_read("sim:healthy")                             # a second read is refused
    finally:
        assert tools.obd_cancel()["state"] in ("running", "cancelled")
        ctrl.obd_job.join(10)
    assert tools.obd_progress()["state"] == "cancelled"
    assert ctrl.case().events() == []


def test_obd_relink_without_a_connection_explains(ctrl, tools):
    ctrl.ai_control = True
    with pytest.raises(ToolError, match="Nothing to relink"):
        tools.obd_relink()


@pytest.mark.parametrize("call", [
    lambda t: t.add_case_note("observation", "x"),
    lambda t: t.set_suspect("Bad PCM"),
])
def test_write_tools_refuse_while_ai_control_is_off(ctrl, tools, call):
    ctrl.sync(session_name="bench")
    with pytest.raises(ToolError, match="AI control is off"):
        call(tools)
    assert ctrl.case().events() == []


def test_add_case_note_records_an_ai_labeled_event(ctrl, tools):
    ctrl.ai_control = True
    ctrl.sync(session_name="bench")
    r = tools.add_case_note("observation", "  5V reference sags to 3.4 V on cranking  ")
    ev = ctrl.case().events()[0]
    assert r == {"id": "e0001", "type": "observation", "session": "bench"}
    assert (ev.type, ev.text, ev.source) == ("observation", "5V reference sags to 3.4 V on cranking", "mcp")
    assert tools.get_case_summary()["timeline"][0]["source"] == "mcp"


def test_add_case_note_is_limited_to_notes(ctrl, tools):
    ctrl.ai_control = True
    with pytest.raises(ToolError, match="type must be one of observation, symptom, change"):
        tools.add_case_note("measurement", "12.6 V")
    with pytest.raises(ToolError, match="needs some text"):
        tools.add_case_note("observation", "   ")
    assert ctrl.case().events() == []


def test_set_suspect_creates_then_updates_by_title(ctrl, tools):
    ctrl.ai_control = True
    ctrl.sync(session_name="bench")
    first = tools.set_suspect("Bad PCM")
    assert first["suspect_id"] == "h1" and first["updated_existing"] is False
    with pytest.raises(ToolError, match="needs a reason"):
        tools.set_suspect("bad pcm", "ruled_out")                    # a status change needs the reason
    second = tools.set_suspect("  bad PCM ", "ruled_out", "second PCM behaved the same")
    assert second["suspect_id"] == "h1" and second["updated_existing"] is True
    assert tools.set_suspect("Chafed harness")["suspect_id"] == "h2"
    board = ctrl.case().hypotheses()
    assert board["h1"].data["status"] == "ruled_out" and board["h1"].source == "mcp"
    with pytest.raises(ToolError, match="unknown hypothesis status"):
        tools.set_suspect("Chafed harness", "maybe")
    with pytest.raises(ToolError, match="needs a title"):
        tools.set_suspect("  ")


def finish_watch(ctrl):
    ctrl.watch_job.join(10)
    assert not ctrl.watch_job.running


def test_watch_tools_are_gated(ctrl, tools):
    for call in (lambda: tools.obd_watch(), lambda: tools.obd_watch_stop(), lambda: tools.obd_watch_cancel()):
        with pytest.raises(ToolError, match="AI control is off"):
            call()
    assert ctrl.watch_job is None


def test_obd_watch_runs_and_get_watch_returns_a_downsampled_series(ctrl, tools):
    ctrl.ai_control = True
    ctrl.sync(session_name="bench")
    with pytest.raises(ToolError, match="Connect to the adapter first"):
        tools.obd_watch()
    assert tools.obd_connect("sim:cranking_rpm_seen")["state"] == "connected"
    try:
        r = tools.obd_watch("raw", 1.0, "crank")
        assert r["started"] is True and r["mode"] == "raw" and r["session"] == "bench"
        assert "monitoring" in r["message"] and "crank" in r["message"]
        finish_watch(ctrl)
        [w] = tools.list_watches()["watches"]
        assert w["label"] == "[AI] crank" and w["mode"] == "raw" and w["sim"] is True
        assert "OBD watch 001" in w["headline"]
        full = tools.get_watch("001", max_points=5000)
        assert full["watch"]["id"] == "001" and full["summary"]["frames"] > 20 and "0C9" in full["ids"]
        assert full["returned"] == full["samples"] and full["series_source"].endswith("(best-effort)")
        small = tools.get_watch("001", max_points=20)
        assert small["samples"] == full["samples"] and small["returned"] <= 20
        assert max(v for _, v in small["engine_speed_series"]) == max(v for _, v in full["engine_speed_series"])
        assert all(0 <= t < 3 for t, _ in small["engine_speed_series"])
        json.dumps(small, allow_nan=False)

        assert tools.obd_watch("poll", 1.0)["mode"] == "poll"
        finish_watch(ctrl)
        poll = tools.get_watch("002")
        assert poll["series_source"] == "010C (official)" and poll["samples"] > 0 and poll["ids"] == {}
        assert [w["id"] for w in tools.list_watches()["watches"]] == ["001", "002"]
    finally:
        tools.obd_disconnect()


def test_obd_watch_stop_and_cancel(ctrl, tools):
    ctrl.ai_control = True
    ctrl.sync(session_name="bench")
    tools.obd_connect("sim:cranking_rpm_seen")
    try:
        tools.obd_watch("raw", 60)
        assert ctrl.watch_job.monitoring.wait(3)
        tools.obd_watch_stop()
        finish_watch(ctrl)
        assert ctrl.watch_job.state == "stopped" and len(tools.list_watches()["watches"]) == 1
        tools.obd_watch("raw", 60)
        tools.obd_watch_cancel()
        finish_watch(ctrl)
        assert len(tools.list_watches()["watches"]) == 1
    finally:
        tools.obd_disconnect()


def test_get_watch_unknown_id_lists_real_ones(ctrl, tools):
    ctrl.ai_control = True
    ctrl.sync(session_name="bench")
    tools.obd_connect("sim:cranking_rpm_seen")
    try:
        tools.obd_watch("poll", 1.0)
        finish_watch(ctrl)
    finally:
        tools.obd_disconnect()
    with pytest.raises(ToolError, match=r"no watch '999'.*Watches: 001\."):
        tools.get_watch("999")
    with pytest.raises(ToolError, match="no watch"):
        tools.get_watch("../x")
    with pytest.raises(ToolError, match="There is no session"):
        tools.get_watch("001", session="nope")


@pytest.mark.parametrize("n", [0, 9, 5001, -1])
def test_get_watch_rejects_silly_max_points(tools, n):
    with pytest.raises(ToolError, match="max_points must be between 10 and 5000"):
        tools.get_watch("001", max_points=n)


def test_watch_progress_idle_and_running(ctrl, tools):
    assert tools.watch_progress() == {"state": "idle", "running": False, "monitoring": False}
    ctrl.ai_control = True
    ctrl.sync(session_name="bench")
    tools.obd_connect("sim:cranking_rpm_seen")
    try:
        tools.obd_watch("raw", 60)
        assert ctrl.watch_job.monitoring.wait(3)
        p = tools.watch_progress()
        assert p["running"] is True and p["monitoring"] is True and p["session"] == "bench"
        assert {"state", "seconds", "lines", "frames", "latest_rpm", "max_rpm", "message"} <= set(p)
        assert p["mode"] == "raw" and p["rpm_source"] == "decoded from raw CAN frames (best-effort, ID 0C9)"
        with pytest.raises(ToolError, match="An OBD watch is running"):
            tools.obd_disconnect()
        tools.obd_watch_stop()
        finish_watch(ctrl)
        p = tools.watch_progress()
        assert p["running"] is False and p["state"] == "stopped" and p["lines"] > 0
        json.dumps(p, allow_nan=False)
        tools.obd_watch("poll", 1)
        finish_watch(ctrl)
        p = tools.watch_progress()
        assert p["mode"] == "poll" and p["rpm_source"] == "010C (official)"
    finally:
        tools.obd_disconnect()


def test_arm_capture_with_watch_links_them(ctrl, tools):
    ctrl.ai_control = True
    ctrl.sync(mode="Simulator", scenario="ref_sag", session_name="bench")
    tools.obd_connect("sim:cranking_rpm_seen")
    try:
        r = tools.arm_capture("linked", watch=True)
        assert r["armed"] is True and r["watch"] is True and r["watch_note"] == ""
        assert "get_capture_progress" in r["message"] and "'armed'" in r["message"]
        assert "Tell the owner to crank the engine now" not in r["message"]
        ctrl.job.join(30)
        assert ctrl.job.state == "done", ctrl.job.message
        finish_watch(ctrl)
        [cap] = tools.list_captures()["captures"]
        [w] = tools.list_watches()["watches"]
        assert tools.get_watch(w["id"])["watch"]["capture_id"] == cap["id"]
        assert w["headline"].startswith("OBD watch")
    finally:
        tools.obd_disconnect()
    # without a connection the capture still runs and the reply carries the reason
    ctrl.sync(session_name="bench2")
    r = tools.arm_capture("alone", watch=True)
    ctrl.job.join(30)
    assert ctrl.job.state == "done" and tools.list_watches()["watches"] == []
    assert "Connect to the adapter first" in ctrl.snapshot()["watch_note"]
    assert r["armed"] is True
