import re

import pytest

from crank import controller as ctl
from crank.model import CaptureSettings, Thresholds
from crank.store import SessionStore


@pytest.fixture
def c(tmp_path):
    return ctl.Controller(SessionStore(tmp_path), poll_s=0.0)


def _finish(c, timeout=10):
    c.job.join(timeout)
    return c.snapshot()


def test_arm_simulator_completes_saves_and_reports_findings(c):
    c.sync(mode="Simulator", scenario="ref_sag", session_name="bench")
    assert c.arm("after reground", "note") is None
    snap = _finish(c)
    assert snap["state"] == "done" and not snap["running"]
    assert ("fail", "5V reference sags") in [(f["severity"], f["title"][:17]) for f in snap["findings"]]
    caps = c.store.open("bench").list_captures()
    assert [x["label"] for x in caps] == ["after reground"] and snap["saved_id"] == caps[0]["id"]


def test_second_arm_while_running_is_refused(c):
    c.sim_fault = "stuck_waiting"
    c.sync(mode="Simulator", scenario="healthy")
    assert c.arm() is None
    try:
        assert "already running" in c.arm()
        assert c.snapshot()["running"]
    finally:
        c.cancel()
        c.job.join(5)
    assert c.snapshot()["state"] == "cancelled"


def test_real_mode_without_connection_returns_message_and_starts_nothing(c):
    c.sync(mode="Real scope")
    assert "Connect to the scope" in c.arm()
    assert c.job is None and c.snapshot()["state"] == "idle"


def test_snapshot_describes_what_will_run(c):
    c.sync(mode="Simulator", scenario="runt_pulses", session_name="s1")
    snap = c.snapshot()
    assert snap["scenario"] == "runt_pulses" and snap["session_name"] == "s1"
    assert snap["mode"] == "Simulator"


def test_sync_ignores_unknown_fields_and_none(c):
    c.sync(mode="Simulator", bogus=1, scenario=None)
    assert c.scenario == "healthy" and not hasattr(c, "bogus")


def test_settings_and_thresholds_are_passed_to_the_job(c):
    s, th = CaptureSettings(seconds=2.0), Thresholds(teeth=24)
    c.sync(mode="Simulator", settings=s, thresholds=th)
    c.arm()
    _finish(c)
    assert c.job.settings is s and c.job.thresholds is th


def test_lan_ip_looks_like_an_address():
    assert re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ctl.lan_ip()) or ctl.lan_ip() == "localhost"


def test_get_controller_is_a_shared_singleton(tmp_path, monkeypatch):
    monkeypatch.setenv("CRANK_SESSIONS_DIR", str(tmp_path))
    ctl.reset_controller()
    assert ctl.get_controller() is ctl.get_controller()
    ctl.reset_controller()
