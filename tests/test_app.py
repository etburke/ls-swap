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
    at.button(key="arm").click().run()
    for _ in range(10):
        if at.session_state["job"].state in ("done", "error"):
            break
        at.run()
    return at.session_state["job"]


def test_app_loads_with_simulator_default(at):
    assert at.sidebar.radio(key="mode").value == "Simulator"
    assert at.session_state["page"] == "Capture"


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
