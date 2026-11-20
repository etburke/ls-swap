import time

import pytest

from crank.case import Case
from crank.controller import Controller
from crank.store import SessionStore

BUSY = "An OBD read or watch is already running."


def ctl(tmp_path, scenario="cranking_rpm_seen", heartbeat_s=60):
    c = Controller(SessionStore(tmp_path / "sessions"), heartbeat_s=heartbeat_s)
    if scenario:
        assert c.connect(scenario) is None
    return c


def finish(c):
    c.watch_job.join(10)
    assert not c.watch_job.running
    return c.watch_job


def lock_is_free(c) -> bool:
    if not c._obd_lock.acquire(blocking=False):
        return False
    c._obd_lock.release()
    return True


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_watch_needs_a_connection(tmp_path):
    c = ctl(tmp_path, scenario=None)
    assert c.start_watch() == "Connect to the adapter first."
    assert c.start_watch("sniff", 500) == "Connect to the adapter first."      # checked first
    assert c.watch_job is None and lock_is_free(c)
    assert c.connection_status()["watching"] is False


def test_watch_records_into_the_case_and_releases_the_lock(tmp_path):
    c = ctl(tmp_path)
    assert c.start_watch("raw", 1.0, label="crank") is None
    assert c.connection_status()["watching"] is True
    job = finish(c)
    assert job.state == "done" and job.meta["label"] == "crank" and job.meta["sim"] is True
    assert job.meta["adapter"] == c.connection.adapter.label()
    assert lock_is_free(c) and c.connection_status()["watching"] is False
    [ev] = c.case().events()
    assert ev.type == "watch" and ev.refs == {"watch": job.meta["id"]} and ev.data["sim"] is True
    assert ev.data["mode"] == "raw" and ev.data["summary"]["engine_speed"]["max"] > 0
    assert c.start_watch("poll", 0.5) is None
    assert finish(c).state == "done"
    assert [e.data["mode"] for e in c.case().events()] == ["raw", "poll"]
    assert [w["id"] for w in c.case().session.list_watches()] == ["001", "002"]
    c.disconnect()


def test_lock_is_released_when_recording_into_the_case_fails(tmp_path, monkeypatch):
    c = ctl(tmp_path)

    def broken(self, meta):
        raise OSError("disk full")
    monkeypatch.setattr(Case, "append_watch", broken)
    assert c.start_watch("raw", 0.3) is None
    job = finish(c)
    assert "disk full" in job.message and lock_is_free(c)
    assert c.case().session.list_watches()                     # the watch itself was kept
    c.disconnect()


def test_watch_blocks_reads_and_a_second_watch(tmp_path):
    c = ctl(tmp_path)
    assert c.start_watch("raw", 5.0) is None
    assert c.watch_job.monitoring.wait(3)
    assert c.start_obd("x") == "An OBD read is already running."
    assert c.start_watch("raw", 1.0) == BUSY
    assert "read is running" in c.connect("healthy")
    c.stop_watch()
    job = finish(c)
    assert job.state == "stopped" and job.meta["end_reason"] == "stopped" and job.frames > 0
    assert [e.type for e in c.case().events()] == ["watch"]
    r, err = c.read_obd("x")
    assert err is None
    c.disconnect()


def test_heartbeat_does_not_ping_during_a_watch(tmp_path):
    c = ctl(tmp_path, heartbeat_s=0.05)
    log = c.traffic.path
    assert wait_for(lambda: log.exists() and "hb >> ATRV" in log.read_text())
    assert c.start_watch("raw", 1.0) is None
    finish(c)
    text = log.read_text()
    during = text[text.index("watch stream started"):text.index("watch stream ended")]
    assert "hb >> ATRV" not in during
    assert wait_for(lambda: "hb >> ATRV" in log.read_text()[text.index("watch stream ended"):])
    c.disconnect()


@pytest.mark.parametrize("scenario", ["cranking_rpm_seen", "monitor_overflow"])
def test_a_read_after_a_raw_watch_gets_normal_replies(tmp_path, scenario):
    c = ctl(tmp_path, scenario)
    assert c.start_watch("raw", 1.0) is None
    job = finish(c)
    assert job.frames > 0 and not job.restore_failed
    r, err = c.read_obd("x")
    assert err is None and any(v.value is not None for v in r.live)
    c.disconnect()


def test_watch_writes_to_the_session_it_started_on(tmp_path):
    c = ctl(tmp_path)
    assert c.start_watch("raw", 1.0) is None
    c.sync(session_name="other")
    job = finish(c)
    assert job.state == "done"
    assert [e.type for e in Case(c.store.open("default")).events()] == ["watch"]
    assert c.case().events() == [] and c.case().session.list_watches() == []
    assert c.store.open("default").list_watches()[0]["id"] == job.meta["id"]
    c.disconnect()


def test_delete_session_refused_while_watching(tmp_path):
    c = ctl(tmp_path)
    assert c.start_watch("raw", 5.0) is None
    assert c.delete_session("default") == "A watch is running on this session; wait for it or stop it first."
    c.cancel_watch()
    job = finish(c)
    assert job.state == "cancelled" and c.case().events() == [] and lock_is_free(c)
    assert c.delete_session("default") is None
    c.disconnect()


def test_simulated_watch_into_a_real_case_is_refused(tmp_path):
    c = ctl(tmp_path)
    c.case().append("code_read", "", {"source": "obd", "sim": False, "codes": []})
    assert c.start_watch() == "This case already has real OBD reads. Start a new case to use the simulator."
    assert c.watch_job is None and lock_is_free(c) and c.case().session.list_watches() == []
    c.disconnect()


def test_reset_mid_watch_leaves_the_connection_cold(tmp_path):
    c = ctl(tmp_path, "reset_mid_watch")
    assert not c.connection.cold
    assert c.start_watch("raw", 1.6) is None
    job = finish(c)
    assert job.meta["end_reason"] == "reset" and c.connection.cold and lock_is_free(c)
    [ev] = c.case().events()
    assert ev.data["end_reason"] == "reset" and ev.data["notes"]
    c.disconnect()


def test_a_failed_restore_leaves_the_connection_cold(tmp_path):
    c = ctl(tmp_path)
    inner = c.connection._inner
    real_send = inner.send
    inner.send = lambda cmd, timeout=5.0: "?" if cmd == "ATCAF1" else real_send(cmd, timeout)
    assert c.start_watch("raw", 0.3) is None
    job = finish(c)
    assert job.restore_failed and c.connection.cold and lock_is_free(c)
    c.disconnect()


def test_bad_mode_and_length_are_refused(tmp_path):
    c = ctl(tmp_path)
    assert c.start_watch("sniff") == "Mode must be 'raw' or 'poll'."
    for seconds in (0, -1, 120.5, 500, float("nan")):
        assert c.start_watch("raw", seconds) == "Watch length must be between 1 and 120 seconds.", seconds
    assert c.watch_job is None and lock_is_free(c)
    c.disconnect()
