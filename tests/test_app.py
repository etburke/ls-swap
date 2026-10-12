import os

import pytest
from streamlit.testing.v1 import AppTest

from crank.controller import get_controller, reset_controller

APP = os.path.join(os.path.dirname(__file__), "..", "app.py")


@pytest.fixture
def at(tmp_path, monkeypatch):
    monkeypatch.setenv("CRANK_SESSIONS_DIR", str(tmp_path))
    monkeypatch.setenv("CRANK_POLL_S", "0")
    monkeypatch.setenv("CRANK_REMOTE_REFRESH_S", "0")
    reset_controller()
    app = AppTest.from_file(APP, default_timeout=60)
    app.run()
    assert not app.exception
    yield app
    reset_controller()


def _arm_and_finish(at):
    at.sidebar.radio(key="page").set_value("Capture").run()
    at.button(key="arm").click().run()
    for _ in range(10):
        if at.session_state["job"].state in ("done", "error"):
            break
        at.run()
    return at.session_state["job"]


def test_app_loads_with_simulator_default(at):
    assert at.sidebar.radio(key="mode").value == "Simulator"
    assert at.session_state["page"] == "Case"          # a case with no captures lands on Case


def test_arm_simulator_completes_and_saves(at):
    job = _arm_and_finish(at)
    assert job.state == "done" and job.analysis.codes == {"signal_clean"}
    assert at.session_state["store"].open("default").list_captures()


def test_results_page_shows_verdict_for_faulty_scenario(at):
    at.sidebar.selectbox(key="scenario").select("ref_sag").run()
    _arm_and_finish(at)
    at.sidebar.radio(key="page").set_value("Results").run()
    assert not at.exception
    assert any("5V reference sags" in m.value for m in at.markdown)


def test_results_page_empty_session_is_friendly(at):
    at.sidebar.radio(key="page").set_value("Results").run()
    assert not at.exception and any("No captures" in i.value for i in at.info)


def test_guided_step_saves_reading(at):
    at.sidebar.radio(key="page").set_value("Guided tests").run()
    at.number_input(key="f_static_battery_v").set_value(12.6).run()
    at.button(key="save_static").click().run()
    assert at.session_state["store"].open("default").readings()["static"]["battery_v"] == 12.6


def test_real_scope_without_connection_shows_error(at):
    at.sidebar.radio(key="mode").set_value("Real scope").run()
    at.sidebar.radio(key="page").set_value("Capture").run()
    at.button(key="arm").click().run()
    assert any("Connect to the scope first" in e.value for e in at.error)


def test_compare_two_captures(at):
    _arm_and_finish(at)
    at.sidebar.selectbox(key="scenario").select("runt_pulses").run()
    _arm_and_finish(at)
    at.sidebar.radio(key="page").set_value("Compare").run()
    assert not at.exception


def test_self_check_button_reports_each_setting(at):
    from tests.test_scope import FakeVisa
    from crank.scope import RigolScope
    at.sidebar.radio(key="mode").set_value("Real scope").run()
    get_controller().scope = RigolScope(inst=FakeVisa())
    at.run()
    at.sidebar.button(key="self_check").click().run()
    assert not at.exception
    assert any("memory depth" in s.value for s in at.sidebar.success)


def test_scope_buttons_are_locked_while_a_capture_runs(at):
    from tests.test_scope import FakeVisa
    from crank.capture import CaptureJob
    from crank.model import CaptureSettings
    from crank.scope import RigolScope, SimScope
    job = CaptureJob(SimScope("healthy", fault="stuck_waiting"), CaptureSettings(wait=30), poll_s=0.01)
    job.start()
    try:
        at.sidebar.radio(key="mode").set_value("Real scope").run()
        get_controller().scope = RigolScope(inst=FakeVisa())
        get_controller().job = job
        at.sidebar.radio(key="page").set_value("Results").run()
        assert at.sidebar.button(key="self_check").disabled
        assert at.sidebar.button(key="connect").disabled
    finally:
        job.cancel(); job.join(5)


def _remote(tmp_path, monkeypatch=None):
    app = AppTest.from_file(APP, default_timeout=60)
    app.query_params["page"] = "remote"
    app.run()
    return app


def test_remote_page_shows_arm_and_no_laptop_navigation(at):
    r = _remote(None)
    assert not r.exception
    assert r.button(key="remote_arm") is not None
    assert not r.sidebar.radio


def test_remote_arm_runs_a_capture_the_laptop_can_see(at):
    r = _remote(None)
    r.text_input(key="remote_label").set_value("from phone").run()
    r.button(key="remote_arm").click().run()
    get_controller().job.join(10)
    r.run()
    assert get_controller().snapshot()["state"] == "done"
    assert any("Crank signal looks clean" in m.value for m in r.markdown)
    assert get_controller().store.open("default").list_captures()[0]["label"] == "from phone"
    at.run()                                           # the laptop session sees the phone's job
    assert at.session_state["job"].state == "done"


def test_remote_arm_in_real_mode_without_connection_shows_message(at):
    at.sidebar.radio(key="mode").set_value("Real scope").run()
    r = _remote(None)
    r.button(key="remote_arm").click().run()
    assert any("Connect to the scope" in e.value for e in r.error)


def test_remote_shows_cancel_while_running(at):
    get_controller().sim_fault = "stuck_waiting"
    r = _remote(None)
    r.button(key="remote_arm").click().run(timeout=5)
    try:
        assert r.button(key="remote_cancel") is not None
        assert r.button(key="remote_arm").disabled
    finally:
        get_controller().cancel()
        get_controller().job.join(5)


def test_laptop_sidebar_shows_the_phone_url(at):
    assert any("page=remote" in c.value for c in at.sidebar.code)


# --- case file
def _case():
    return get_controller().case()


def _go(at, page):
    at.sidebar.radio(key="page").set_value(page).run()
    assert not at.exception


def test_case_page_adds_each_kind_of_entry(at):
    at.selectbox(key="qa_type").select("symptom").run()
    at.text_area(key="qa_text_0").set_value("no RPM while cranking").run()
    at.button(key="qa_add").click().run()
    at.selectbox(key="qa_type").select("measurement").run()
    at.text_input(key="qa_mname_1").set_value("Battery").run()
    at.number_input(key="qa_mval_1").set_value(12.6).run()
    at.text_input(key="qa_munit_1").set_value("V").run()
    at.button(key="qa_add").click().run()
    at.selectbox(key="qa_type").select("code_read").run()
    at.text_input(key="qa_codes_2").set_value("p2119, P0335").run()
    at.button(key="qa_add").click().run()
    at.selectbox(key="qa_type").select("hypothesis").run()
    at.text_input(key="qa_htitle_3").set_value("Bad crank sensor").run()
    at.button(key="qa_add").click().run()
    assert not at.exception
    kinds = [e.type for e in _case().events()]
    assert kinds == ["symptom", "measurement", "code_read", "hypothesis"]
    assert [c["code"] for c in _case().events()[2].data["codes"]] == ["P2119", "P0335"]


def test_case_page_shows_validation_errors_and_writes_nothing(at):
    at.selectbox(key="qa_type").select("symptom").run()
    at.button(key="qa_add").click().run()
    assert any("needs some text" in e.value for e in at.error)
    assert _case().events() == []


def test_hypothesis_needs_a_reason_to_be_ruled_out_then_moves(at):
    h = _case().set_hypothesis(None, "Bad PCM")
    at.run()
    hid = h.data["hid"]
    at.selectbox(key=f"hs_default_{hid}").select("ruled_out").run()
    at.button(key=f"hu_default_{hid}").click().run()
    assert any("needs a reason" in e.value for e in at.error)
    at.text_input(key=f"hr_default_{hid}").set_value("second PCM behaved the same").run()
    at.button(key=f"hu_default_{hid}").click().run()
    assert _case().hypotheses()[hid].data["status"] == "ruled_out"


def test_suggestions_come_from_the_latest_verdict(at):
    at.sidebar.selectbox(key="scenario").select("ref_sag").run()
    _arm_and_finish(at)
    _go(at, "Case")
    assert any("5V reference circuit" in b.label for b in at.button)
    next(b for b in at.button if "5V reference circuit" in b.label).click().run()
    assert [e.data["title"] for e in _case().hypotheses().values()] == [
        "5V reference circuit and anything sharing it"]


def test_report_preview_flags_gaps_and_offers_downloads(at):
    at.button(key="gen_report").click().run()
    assert any("Nothing has been recorded" in w.value for w in at.warning)
    _case().append("symptom", "stalls when hot")
    at.button(key="gen_report").click().run()
    assert any("Gaps the report will show" in i.value for i in at.info)
    assert "stalls when hot" in at.code[0].value


def test_vehicle_profile_saves(at):
    at.text_input(key="veh_make_default").set_value("Toyota").run()
    at.button(key="save_vehicle").click().run()
    assert _case().vehicle().make == "Toyota"


def test_capture_and_guided_steps_reach_the_case_log(at):
    _arm_and_finish(at)
    get_controller().job.join(5)
    assert [e.type for e in _case().events()] == ["capture"]
    _go(at, "Guided tests")
    at.number_input(key="f_static_battery_v").set_value(12.6).run()
    at.button(key="save_static").click().run()
    assert [e.type for e in _case().events()].count("measurement") == 4


def test_results_can_log_the_verdict_as_an_observation(at):
    _arm_and_finish(at)
    _go(at, "Results")
    at.button(key="log_verdict").click().run()
    obs = [e for e in _case().events() if e.type == "observation"]
    assert obs and "Crank signal looks clean" in obs[0].text and obs[0].refs["capture"] == "001"


def test_compare_links_two_captures_to_a_change(at):
    _arm_and_finish(at)
    at.sidebar.selectbox(key="scenario").select("ref_sag").run()
    _arm_and_finish(at)
    get_controller().job.join(5)
    _go(at, "Compare")
    at.selectbox(key="cmp_cap_A").select("001").run()
    at.selectbox(key="cmp_cap_B").select("002").run()
    at.text_input(key="cmp_change").set_value("swapped sensor").run()
    at.button(key="cmp_mark").click().run()
    ch = [e for e in _case().events() if e.type == "change"]
    assert ch and ch[0].refs == {"before": "001", "after": "002"}


def test_phone_quick_add_logs_observation_and_change(at):
    r = _remote(None)
    r.text_input(key="remote_note").set_value("fan kicked on").run()
    r.button(key="remote_obs").click().run()
    r.text_input(key="remote_note").set_value("cleaned connector").run()
    r.button(key="remote_change").click().run()
    assert [(e.type, e.text) for e in _case().events()] == [("observation", "fan kicked on"),
                                                           ("change", "cleaned connector")]
    r.text_input(key="remote_note").set_value("").run()
    r.button(key="remote_obs").click().run()
    assert any("needs some text" in e.value for e in r.error)
