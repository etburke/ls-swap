import time

import pytest

from crank.case import Case
from crank.controller import Controller
from crank.scope import SimScope
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
    assert c.start_obd("x") == "An OBD watch is running; wait for it or stop it first."   # was the read text
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


# capture-linked watches

def linked(tmp_path, scenario="cranking_rpm_seen"):
    c = Controller(SessionStore(tmp_path / "sessions"), poll_s=0.02, heartbeat_s=60)
    if scenario:
        assert c.connect(scenario) is None
    return c


def finish_capture(c):
    c.job.join(10)
    assert not c.job.running
    return c.job


def test_linked_watch_is_monitoring_before_the_scope_arms(tmp_path, monkeypatch):
    c = linked(tmp_path)
    seen = []
    real_arm = SimScope.arm

    def arm(self):
        seen.append((c.watch_job.monitoring.is_set(), c.job.state))
        real_arm(self)
    monkeypatch.setattr(SimScope, "arm", arm)
    t0 = time.monotonic()
    assert c.arm("crank", watch=True) is None
    assert time.monotonic() - t0 < 0.5                         # the wait happens on the capture thread
    cap = finish_capture(c)
    watch = finish(c)
    assert seen == [(True, "configuring")]
    assert cap.state == "done" and cap.note == "" and c.snapshot()["watch_note"] == ""
    assert watch.meta["end_reason"] == "until" and lock_is_free(c)
    c.disconnect()


def test_linked_watch_stops_after_trigger_plus_record_plus_tail(tmp_path, monkeypatch):
    c = linked(tmp_path)
    c.settings.seconds = 0.2
    real_fetch = SimScope.fetch

    def slow_fetch(self, s):                                   # a long download: the watch must not wait for it
        wait_for(lambda: not c.watch_job.running, 5.0)
        return real_fetch(self, s)
    monkeypatch.setattr(SimScope, "fetch", slow_fetch)
    assert c.arm("crank", watch=True) is None
    assert wait_for(lambda: c.watch_job is not None)          # started on the capture's thread
    watch = finish(c)
    cap = finish_capture(c)
    assert watch.meta["end_reason"] == "until" and cap.state == "done"
    assert watch.meta["trigger_t"] == cap.triggered_at
    assert 2.2 <= watch.finished_at - cap.triggered_at < 2.8
    c.disconnect()


def _cross_referenced(c, cap, watch):
    sess = c.case().session
    assert watch.meta["capture_id"] == cap.saved_id and watch.meta["trigger_t"] == cap.triggered_at
    [wmeta] = sess.list_watches()
    assert wmeta["capture_id"] == cap.saved_id and wmeta["trigger_t"] == cap.triggered_at
    [cmeta] = sess.list_captures()
    assert cmeta["watch_id"] == watch.meta["id"]
    return {e.type: e for e in c.case().events()}


def test_linked_watch_and_capture_cross_reference(tmp_path, monkeypatch):
    # the capture saves first (the watch ends when the capture is done)
    c = linked(tmp_path / "a")
    assert c.arm("first", watch=True) is None
    cap, watch = finish_capture(c), finish(c)
    assert cap.saved_id, (cap.state, cap.message, cap.hook_error, watch.state, watch.message)
    evs = _cross_referenced(c, cap, watch)
    assert evs["capture"].refs == {"capture": cap.saved_id}
    assert evs["watch"].refs == {"watch": watch.meta["id"], "capture": cap.saved_id}
    c.disconnect()

    # the watch saves first (stopped while the scope is still downloading)
    c = linked(tmp_path / "b")
    real_fetch = SimScope.fetch

    def slow_fetch(self, s):
        c.stop_watch()
        wait_for(lambda: not c.watch_job.running, 5.0)
        return real_fetch(self, s)
    monkeypatch.setattr(SimScope, "fetch", slow_fetch)
    assert c.arm("second", watch=True) is None
    cap, watch = finish_capture(c), finish(c)
    assert cap.saved_id, (cap.state, cap.message, cap.hook_error, watch.state, watch.message)
    evs = _cross_referenced(c, cap, watch)
    assert evs["watch"].refs == {"watch": watch.meta["id"]}
    assert evs["capture"].refs == {"capture": cap.saved_id, "watch": watch.meta["id"]}
    c.disconnect()


def test_capture_without_connection_still_runs_with_a_note(tmp_path):
    c = linked(tmp_path, scenario=None)
    assert c.arm("x", watch=True) is None
    cap = finish_capture(c)
    assert cap.state == "done" and cap.saved_id
    assert c.snapshot()["watch_note"] == "Connect to the adapter first."
    assert c.watch_job is None and lock_is_free(c)


def test_simulated_capture_is_not_linked_to_a_real_watch(tmp_path):
    c = linked(tmp_path)
    c.connection.kind = "real"                                 # a real adapter, but the scope is the simulator
    assert c.arm("x", watch=True) is None
    cap = finish_capture(c)
    assert cap.state == "done"
    assert c.snapshot()["watch_note"] == ("A simulated capture is only linked to a simulated watch; "
                                          "the adapter connection is real.")
    assert c.watch_job is None and lock_is_free(c)
    c.connection.kind = "simulated"
    c.disconnect()


def test_capture_cancel_stops_and_keeps_the_watch(tmp_path):
    c = linked(tmp_path)
    c.sim_fault = "stuck_waiting"
    assert c.arm("x", watch=True) is None
    assert wait_for(lambda: c.job.state == "armed")
    c.cancel()
    cap, watch = finish_capture(c), finish(c)
    assert cap.state == "cancelled" and watch.state == "stopped" and watch.meta["end_reason"] == "stopped"
    assert watch.meta["capture_id"] is None and watch.lines > 0
    assert f"linked capture did not save: {cap.message}" in watch.meta["notes"]
    assert [e.type for e in c.case().events()] == ["watch"]
    assert lock_is_free(c)
    c.disconnect()


def test_failed_capture_keeps_the_watch_with_a_note(tmp_path):
    c = linked(tmp_path)
    c.sim_fault = "fetch_error"
    assert c.arm("x", watch=True) is None
    cap, watch = finish_capture(c), finish(c)
    assert cap.state == "error" and cap.saved_id is None
    assert watch.meta["capture_id"] is None and watch.meta["trigger_t"] == cap.triggered_at
    note = "linked capture did not save: Simulated download failure."
    assert note in watch.meta["notes"]
    [wmeta] = c.case().session.list_watches()
    assert note in wmeta["notes"]
    [ev] = c.case().events()
    assert ev.type == "watch" and ev.refs == {"watch": watch.meta["id"]} and note in ev.data["notes"]
    c.disconnect()


def test_capture_failing_after_its_watch_saved_still_notes_the_watch(tmp_path, monkeypatch):
    import crank.controller as controller
    monkeypatch.setattr(controller, "LINKED_TAIL_S", 0.1)
    c = linked(tmp_path)
    c.settings.seconds = 0.1
    c.sim_fault = "fetch_error"
    real_fetch = SimScope.fetch

    def slow_failing_fetch(self, s):                           # the watch is saved before the download fails
        wait_for(lambda: not c.watch_job.running, 5.0)
        return real_fetch(self, s)
    monkeypatch.setattr(SimScope, "fetch", slow_failing_fetch)
    assert c.arm("x", watch=True) is None
    cap = finish_capture(c)
    watch = finish(c)
    assert cap.state == "error" and watch.meta["end_reason"] == "until"
    note = "linked capture did not save: Simulated download failure."
    assert watch.meta["notes"].count(note) == 1
    [wmeta] = c.case().session.list_watches()
    assert wmeta["notes"].count(note) == 1 and wmeta["capture_id"] is None
    c.disconnect()


# the connection after a watch, refusals while one runs, and link write failures

def test_a_cancelled_watch_leaves_the_connection_cold(tmp_path):
    c = ctl(tmp_path)
    assert c.start_watch("raw", 5.0) is None
    assert c.watch_job.monitoring.wait(3)
    c.cancel_watch()
    assert finish(c).state == "cancelled" and c.connection.cold and lock_is_free(c)
    c.disconnect()


def test_a_watch_that_fails_leaves_the_connection_cold(tmp_path):
    from crank.obd.errors import ObdTimeout
    c = ctl(tmp_path)

    def broken(*a, **kw):
        raise ObdTimeout("The adapter did not answer.")
    c.connection._inner.stream = broken
    assert c.start_watch("raw", 1.0) is None
    job = finish(c)
    assert job.state == "error" and c.connection.cold and lock_is_free(c)
    c.disconnect()


def test_a_silent_vehicle_is_noted_on_the_connection(tmp_path):
    c = ctl(tmp_path)
    c.connection._inner.sc.fault = "no_vehicle"
    assert c.start_watch("raw", 1.0) is None
    job = finish(c)
    assert job.message == "The vehicle did not answer. Is the key on?"
    assert c.connection.vehicle == "no answer" and c.connection.cold
    c.connection._inner.sc.fault = None
    c.disconnect()


def test_controller_reads_the_public_reset_flag():
    import inspect

    import crank.controller as controller
    from crank.obd.watch import WatchJob
    assert isinstance(WatchJob.reset, property)
    assert "job._reset" not in inspect.getsource(controller)


def test_a_read_or_disconnect_during_a_watch_says_a_watch_is_running(tmp_path):
    c = ctl(tmp_path)
    assert c.start_watch("raw", 5.0) is None
    assert c.watch_job.monitoring.wait(3)
    msg = "An OBD watch is running; wait for it or stop it first."
    assert c.start_obd("x") == msg
    assert c.disconnect() == msg and c.connection.state == "connected"
    c.stop_watch()
    finish(c)
    assert c.disconnect() is None and c.connection.state == "disconnected"


@pytest.mark.parametrize("watch_first", [False, True])
def test_a_failing_link_write_never_drops_the_case_events(tmp_path, monkeypatch, watch_first):
    from crank.store import Session

    def broken(self, wid, capture_id):
        raise OSError("disk full")
    monkeypatch.setattr(Session, "link_watch", broken)
    c = linked(tmp_path)
    if watch_first:
        real_fetch = SimScope.fetch

        def slow_fetch(self, s):
            c.stop_watch()
            wait_for(lambda: not c.watch_job.running, 5.0)
            return real_fetch(self, s)
        monkeypatch.setattr(SimScope, "fetch", slow_fetch)
    assert c.arm("x", watch=True) is None
    cap, watch = finish_capture(c), finish(c)
    assert cap.state == "done" and lock_is_free(c)
    evs = {e.type: e for e in c.case().events()}
    assert set(evs) == {"capture", "watch"}
    assert any("disk full" in n for n in watch.meta["notes"])
    c.disconnect()
