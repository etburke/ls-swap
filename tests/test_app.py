import os
import threading
import time

import pytest
from streamlit.testing.v1 import AppTest

from crank.controller import get_controller, reset_controller
from crank.obd.transport import SimTransport

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
    at.selectbox(key=f"hs_default_{hid}_{h.id}").select("ruled_out").run()
    at.button(key=f"hu_default_{hid}_{h.id}").click().run()
    assert any("needs a reason" in e.value for e in at.error)
    at.text_input(key=f"hr_default_{hid}_{h.id}").set_value("second PCM behaved the same").run()
    at.button(key=f"hu_default_{hid}_{h.id}").click().run()
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
    assert [e.type for e in _case().events()].count("measurement") == 1     # only the field that was filled


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


def test_guided_step_saved_with_one_field_logs_one_measurement(at):
    _go(at, "Guided tests")
    at.number_input(key="f_static_battery_v").set_value(12.6).run()
    at.button(key="save_static").click().run()
    assert [e.data["name"] for e in _case().events() if e.type == "measurement"] == ["Battery voltage"]


def test_quick_add_measurement_needs_a_value(at):
    at.selectbox(key="qa_type").select("measurement").run()
    at.text_input(key="qa_mname_0").set_value("Battery").run()
    at.text_input(key="qa_munit_0").set_value("V").run()
    at.button(key="qa_add").click().run()
    assert any("numeric value" in e.value for e in at.error) and _case().events() == []


def test_a_report_goes_stale_when_the_case_changes(at):
    _case().append("symptom", "stalls")
    at.button(key="gen_report").click().run()
    assert not any("out of date" in w.value for w in at.warning)
    _case().append("observation", "new info")
    at.run()
    assert any("out of date" in w.value for w in at.warning)


def test_case_page_obd_read_records_codes_and_shows_a_result(at):
    at.selectbox(key="obd_scenario").select("p2119_zero_rpm").run()
    at.button(key="obd_read").click().run()
    assert not at.exception
    assert any("P2119 (stored)" in m.value for m in at.markdown)
    assert [e.type for e in get_controller().case().events()].count("code_read") == 1


def test_case_page_obd_failure_shows_an_error_and_records_nothing(at):
    at.selectbox(key="obd_scenario").select("silent_adapter").run()
    at.button(key="obd_read").click().run()
    assert not at.exception and at.error
    assert get_controller().case().events() == []


def test_phone_remote_obd_button(at):
    at.query_params["page"] = "remote"
    at.run()
    at.button(key="remote_obd").click().run()
    assert not at.exception
    assert any("Codes (simulated)" in m.value for m in at.markdown)


def test_logging_the_verdict_twice_adds_one_observation(at):
    _arm_and_finish(at)
    _go(at, "Results")
    at.button(key="log_verdict").click().run()
    at.button(key="log_verdict").click().run()
    obs = [e for e in _case().events() if e.type == "observation" and "verdict" in e.text]
    assert len(obs) == 1 and any("already" in i.value for i in at.info)


def test_change_entry_can_name_its_before_and_after_captures(at):
    _arm_and_finish(at)
    _arm_and_finish(at)
    ids = [c["id"] for c in _case().session.list_captures()]
    assert len(ids) == 2
    _go(at, "Case")
    at.selectbox(key="qa_type").select("change").run()
    at.text_area(key="qa_text_0").set_value("swapped sensor").run()
    at.selectbox(key="qa_before_0").select(ids[0]).run()
    at.selectbox(key="qa_after_0").select(ids[1]).run()
    at.button(key="qa_add").click().run()
    ev = next(e for e in _case().events() if e.type == "change")
    assert ev.refs == {"before": ids[0], "after": ids[1]}


def _waiting_read(monkeypatch, after=0):
    """Make the next OBD read wait once `after` commands were sent. Returns the event that lets it go on.
    after=0 waits before the PCM has answered anything; after=11 waits right after the first 0100 answer."""
    gate = threading.Event()

    class Waiting(SimTransport):
        def send(self, cmd, timeout=5.0):
            if self.n >= after:
                while not gate.is_set():
                    self.check_cancel()
                    time.sleep(0.01)
            return super().send(cmd, timeout)
    monkeypatch.setattr(get_controller(), "_obd_transport", lambda source: Waiting("healthy"))
    return gate


def test_case_page_shows_a_running_read_and_cancel_records_nothing(at, monkeypatch):
    gate = _waiting_read(monkeypatch)
    try:
        at.button(key="obd_read").click().run()
        assert not at.exception
        assert any("Reading from the vehicle" in i.value for i in at.info)
        at.run()                                           # the next poll: the button is drawn disabled
        assert at.button(key="obd_read").disabled
        at.button(key="obd_read_cancel").click().run()
        assert any("Read cancelled; nothing was recorded." in w.value for w in at.warning)
        assert get_controller().case().events() == []
        at.run()
        assert not at.button(key="obd_read").disabled
    finally:
        gate.set()


def test_case_page_stop_keeps_what_was_read(at, monkeypatch):
    gate = _waiting_read(monkeypatch, after=11)
    try:
        at.button(key="obd_read").click().run()
        assert any("Reading from the vehicle" in i.value for i in at.info)
        threading.Timer(0.3, gate.set).start()             # the command in flight finishes, then it stops
        at.button(key="obd_read_stop").click().run()
        assert any("Stopped; kept what was read" in s.value for s in at.success)
        events = [e for e in get_controller().case().events() if e.type == "code_read"]
        assert len(events) == 1 and events[0].data["stopped"] is True
    finally:
        gate.set()


def test_stop_before_the_pcm_answers_cancels_on_the_case_page(at, monkeypatch):
    gate = _waiting_read(monkeypatch)
    try:
        at.button(key="obd_read").click().run()
        at.button(key="obd_read_stop").click().run()
        assert any("Read cancelled" in w.value for w in at.warning)
        assert get_controller().case().events() == []
    finally:
        gate.set()


def test_phone_remote_shows_a_running_read_with_both_buttons_and_cancel_stops_it(at, monkeypatch):
    gate = _waiting_read(monkeypatch)
    try:
        at.query_params["page"] = "remote"
        at.run()
        at.button(key="remote_obd").click().run()
        assert any("Reading from the vehicle" in i.value for i in at.info)
        assert at.button(key="remote_obd_stop") and at.button(key="remote_obd_cancel")
        at.button(key="remote_obd_cancel").click().run()
        assert any("Read cancelled" in w.value for w in at.warning)
        assert get_controller().case().events() == []
    finally:
        gate.set()


def test_a_finished_read_is_not_shown_on_another_session(at):
    at.selectbox(key="obd_scenario").select("p2119_zero_rpm").run()
    at.button(key="obd_read").click().run()
    assert any("Recorded in the case" in s.value for s in at.success)
    get_controller().store.open("other")
    at.run()
    at.segmented_control(key="session_pick").set_value("other").run()
    assert not at.exception
    assert not any("Recorded in the case" in s.value for s in at.success)


def test_sessions_are_tabs_with_a_plus_at_the_end(at):
    get_controller().store.open("other")
    at.run()
    tabs = at.segmented_control(key="session_pick")
    assert tabs.options[-1] == "＋" and "other" in tabs.options
    assert tabs.value == at.session_state["session_name"]


def test_plus_tab_creates_and_selects_a_new_session(at):
    at.segmented_control(key="session_pick").set_value("＋").run()
    assert at.session_state["session_name"] != "＋"      # still the previous real session
    at.text_input(key="new_session").set_value("fresh").run()
    at.button(key="create_session").click().run()
    assert not at.exception
    assert at.session_state["session_name"] == "fresh"
    assert at.segmented_control(key="session_pick").value == "fresh"
    assert "fresh" in get_controller().store.list_sessions()


def test_plus_tab_shows_the_name_field_only_when_selected(at):
    assert not [t for t in at.text_input if t.key == "new_session"]
    at.segmented_control(key="session_pick").set_value("＋").run()
    assert at.text_input(key="new_session")
    assert not [t for t in at.sidebar.text_input if t.key == "new_session"]


def test_plus_tab_rejects_a_bad_name_and_stays_on_plus(at):
    before = at.session_state["session_name"]
    at.segmented_control(key="session_pick").set_value("＋").run()
    at.text_input(key="new_session").set_value("   ").run()
    at.button(key="create_session").click().run()
    assert not at.exception
    assert at.session_state["session_name"] == before


@pytest.fixture
def at_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CRANK_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("CRANK_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("CRANK_HEARTBEAT_S", "0.05")
    monkeypatch.setenv("CRANK_POLL_S", "0")
    monkeypatch.setenv("CRANK_REMOTE_REFRESH_S", "0")
    reset_controller()
    app = AppTest.from_file(APP, default_timeout=60)
    app.run()
    assert not app.exception
    yield app
    get_controller().disconnect()
    reset_controller()


def test_sidebar_shows_the_connection_and_its_buttons(at_conn):
    assert any("Disconnected" in m.value for m in at_conn.sidebar.markdown)
    assert at_conn.sidebar.button(key="obd_connect").disabled is False
    assert at_conn.sidebar.button(key="obd_disconnect").disabled is True


def test_connect_from_the_sidebar_with_the_simulator_then_disconnect(at_conn):
    at_conn.sidebar.radio(key="obd_src_mode").set_value("Simulator").run()
    at_conn.sidebar.button(key="obd_connect").click().run()
    assert not at_conn.exception
    assert get_controller().connection_status()["state"] == "connected"
    assert any("Connected" in m.value for m in at_conn.sidebar.markdown)
    assert at_conn.sidebar.button(key="obd_connect").disabled is True
    at_conn.sidebar.button(key="obd_disconnect").click().run()
    assert get_controller().connection_status()["state"] == "disconnected"


def test_a_dropped_connection_shows_dropped_and_relink_brings_it_back(at_conn):
    ctrl = get_controller()
    assert ctrl.connect("heartbeat_drop") is None
    deadline = time.time() + 5
    while time.time() < deadline and ctrl.connection_status()["state"] != "dropped":
        time.sleep(0.05)
    at_conn.run()
    assert any("Dropped" in m.value for m in at_conn.sidebar.markdown)
    at_conn.sidebar.button(key="obd_relink").click().run()
    assert ctrl.connection_status()["state"] == "connected"


def test_the_case_page_says_it_uses_the_open_connection(at_conn):
    get_controller().connect("healthy")
    at_conn.run()
    assert any("Using the open connection" in c.value for c in at_conn.caption)
    at_conn.button(key="obd_read").click().run()
    assert any("Recorded in the case" in s.value for s in at_conn.success)


def test_the_phone_page_shows_the_connection_status(at_conn):
    at_conn.query_params["page"] = "remote"
    at_conn.run()
    assert any("Disconnected" in m.value for m in at_conn.markdown)
    assert at_conn.button(key="remote_obd_connect")


def test_the_log_page_tails_the_log_and_can_hide_heartbeats(at_conn):
    ctrl = get_controller()
    ctrl.traffic.event("hello from the test")
    ctrl.traffic.command("ATRV", tag="hb")
    at_conn.sidebar.radio(key="page").set_value("Log").run()
    assert not at_conn.exception
    code = " ".join(c.value for c in at_conn.code)
    assert "hello from the test" in code and "hb >> ATRV" not in code     # heartbeats hidden by default
    at_conn.toggle(key="log_hide_hb").set_value(False).run()
    assert "hb >> ATRV" in " ".join(c.value for c in at_conn.code)
    at_conn.toggle(key="log_pause").set_value(True).run()
    assert not at_conn.exception


def _delete_buttons(at):
    return [b for b in at.button if b.key in ("delete_session", "delete_confirm", "delete_cancel")]


def test_delete_asks_first_and_cancel_keeps_the_session(at):
    get_controller().store.open("other")
    at.run()
    at.segmented_control(key="session_pick").set_value("other").run()
    at.button(key="delete_session").click().run()
    assert any("other" in w.value and "can't be undone" in w.value for w in at.warning)
    at.button(key="delete_cancel").click().run()
    assert not at.exception and "other" in get_controller().store.list_sessions()
    assert not [b for b in at.button if b.key == "delete_confirm"]


def test_confirmed_delete_removes_the_session_and_selects_another(at):
    get_controller().store.open("other")
    at.run()
    at.segmented_control(key="session_pick").set_value("other").run()
    at.button(key="delete_session").click().run()
    at.button(key="delete_confirm").click().run()
    assert not at.exception
    assert "other" not in get_controller().store.list_sessions()
    assert at.session_state["session_name"] != "other"
    assert "other" not in at.segmented_control(key="session_pick").options
    assert at.segmented_control(key="session_pick").value == at.session_state["session_name"]


def test_switching_tabs_drops_a_pending_delete_confirmation(at):
    get_controller().store.open("other")
    at.run()
    at.segmented_control(key="session_pick").set_value("other").run()
    at.button(key="delete_session").click().run()
    at.segmented_control(key="session_pick").set_value("default").run()
    assert not [b for b in at.button if b.key == "delete_confirm"]


def test_delete_is_hidden_on_the_plus_tab(at):
    at.segmented_control(key="session_pick").set_value("＋").run()
    assert not [b for b in at.button if b.key == "delete_session"]


def test_delete_is_refused_with_a_message_while_a_read_runs(at, monkeypatch):
    gate = _waiting_read(monkeypatch)
    try:
        at.button(key="obd_read").click().run()
        at.button(key="delete_session").click().run()
        at.button(key="delete_confirm").click().run()
        assert not at.exception
        assert any("running" in e.value for e in at.error)
        assert at.session_state["session_name"] in get_controller().store.list_sessions()
    finally:
        gate.set()


def test_remote_url_is_none_without_a_network_address():
    import remote
    assert remote.remote_url("192.168.1.5", 8501) == "http://192.168.1.5:8501/?page=remote"
    assert remote.remote_url("localhost", 8501) is None


def test_qr_svg_is_an_svg_and_depends_on_the_url():
    import remote
    a = remote.qr_svg("http://192.168.1.5:8501/?page=remote")
    assert a.lstrip().startswith("<svg") and "</svg>" in a
    assert a != remote.qr_svg("http://192.168.1.6:8501/?page=remote")


def test_sidebar_offers_a_qr_code_for_the_phone_remote(at, monkeypatch):
    monkeypatch.setattr("crank.controller.lan_ip", lambda: "192.168.1.5")
    at.run()
    assert not at.exception
    assert any("QR code" in e.label for e in at.sidebar.expander)
    assert any("192.168.1.5" in c.value for c in at.sidebar.code)


def test_sidebar_has_no_qr_code_without_a_network_address(at, monkeypatch):
    monkeypatch.setattr("crank.controller.lan_ip", lambda: "localhost")
    at.run()
    assert not at.exception
    assert not any("QR code" in e.label for e in at.sidebar.expander)


def test_ai_access_block_shows_the_endpoint_and_starts_with_control_off(at):
    assert at.sidebar.toggle(key="ai_control").value is False
    assert get_controller().ai_control is False
    codes = [c.value for c in at.sidebar.code]
    assert any(c.startswith("claude mcp add --transport http ls-swap http://127.0.0.1:") and c.endswith("/mcp")
               for c in codes)


def test_the_toggle_switches_ai_control_on_and_off(at):
    at.sidebar.toggle(key="ai_control").set_value(True).run()
    assert get_controller().ai_control is True
    at.sidebar.toggle(key="ai_control").set_value(False).run()
    assert get_controller().ai_control is False


def test_another_tab_rerunning_does_not_flip_the_toggle_back(at):
    at.sidebar.toggle(key="ai_control").set_value(True).run()
    assert get_controller().ai_control is True
    get_controller().ai_control = False                   # turned off elsewhere (another tab)
    at.run()                                              # this tab reruns without the user touching the toggle
    assert get_controller().ai_control is False


def test_a_busy_mcp_port_is_shown_not_fatal(tmp_path, monkeypatch):
    import socket
    monkeypatch.setenv("CRANK_SESSIONS_DIR", str(tmp_path))
    monkeypatch.setenv("CRANK_POLL_S", "0")
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        monkeypatch.setenv("CRANK_MCP_PORT", str(blocker.getsockname()[1]))
        reset_controller()
        app = AppTest.from_file(APP, default_timeout=60)
        app.run()
        assert not app.exception
        assert any("MCP server is not running" in w.value for w in app.sidebar.warning)
    reset_controller()
