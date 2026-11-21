import threading
import time

import pytest

from crank.case import Case
from crank.controller import Controller
from crank.obd.errors import ObdNoAdapter
from crank.obd.transport import SimTransport
from crank.store import SessionStore


def test_controller_read_records_and_returns_the_readout(tmp_path):
    c = Controller(SessionStore(tmp_path))
    r, err = c.read_obd("p2119_zero_rpm")
    assert err is None and r.codes[0]["code"] == "P2119"
    assert [e.type for e in c.case().events()].count("code_read") == 1


@pytest.mark.parametrize("name", ["silent_adapter", "no_vehicle", "garbage", "drops_mid_read"])
def test_controller_failure_returns_a_message_and_appends_nothing(tmp_path, name):
    c = Controller(SessionStore(tmp_path))
    r, err = c.read_obd(name)
    assert r is None and err and c.case().events() == []


from crank.obd import serial_transport
from crank.obd.errors import ObdNoAdapter
from crank.obd.transport import SimTransport


def _serial_via_sim(monkeypatch, scenario="p2119_zero_rpm"):
    sim = SimTransport(scenario)
    sim.open()

    class Fake:
        in_waiting = 0

        def __init__(self):
            self.written, self.queue = [], []

        def write(self, data):
            self.written.append(data)
            self.queue = list((sim.send(data.decode().strip()) + "\r\r>").encode())

        def read(self, n=1):
            return bytes([self.queue.pop(0)]) if self.queue else b""

        def reset_input_buffer(self): pass
        def close(self): pass

    fake = Fake()
    monkeypatch.setattr(serial_transport, "_default_factory", lambda port, baud, timeout: fake)
    return fake


def test_serial_source_reads_and_records_a_real_readout(tmp_path, monkeypatch):
    fake = _serial_via_sim(monkeypatch)
    c = Controller(SessionStore(tmp_path))
    r, err = c.read_obd(("serial", "/dev/cu.fake"))
    assert err is None and r.codes[0]["code"] == "P2119" and r.sim is False
    assert [e.type for e in c.case().events()].count("code_read") == 1
    assert fake.written and fake.written[0] == b"ATZ\r"


def test_serial_auto_with_no_port_returns_a_message(tmp_path, monkeypatch):
    def none(p):
        raise ObdNoAdapter("No OBD adapter port found.")
    monkeypatch.setattr("crank.controller.resolve_port", none)
    c = Controller(SessionStore(tmp_path))
    r, err = c.read_obd(("serial", "auto"))
    assert r is None and "No OBD adapter" in err and c.case().events() == []


def test_unknown_source_is_a_message_not_a_crash(tmp_path):
    r, err = Controller(SessionStore(tmp_path)).read_obd(("bluetooth", "x"))
    assert r is None and err


def test_retry_notes_reach_the_stored_case_event(tmp_path):
    c = Controller(SessionStore(tmp_path))
    r, err = c.read_obd("transient_can_error")
    assert err is None
    ev = [e for e in c.case().events() if e.type == "code_read"][0]
    assert ev.data["notes"] == ["Retried 010C: the adapter reported CAN ERROR."]


def test_a_second_read_during_a_running_one_is_refused(tmp_path):
    from crank.obd.transport import SimTransport
    c = Controller(SessionStore(tmp_path))
    started, release = threading.Event(), threading.Event()

    class Slow(SimTransport):
        def open(self):
            started.set()
            release.wait(5)
            super().open()

    c._obd_transport = lambda source: Slow("healthy")
    out = []
    th = threading.Thread(target=lambda: out.append(c.read_obd("healthy")))
    th.start()
    assert started.wait(5)
    assert c.read_obd("healthy") == (None, "An OBD read is already running.")
    release.set()
    th.join(5)
    assert out[0][1] is None
    assert c.read_obd("healthy")[1] is None                   # released afterwards


def test_the_read_lock_is_released_after_an_obd_error(tmp_path):
    c = Controller(SessionStore(tmp_path))
    assert c.read_obd("silent_adapter")[0] is None
    assert c.read_obd("healthy")[1] is None


def test_the_read_lock_is_released_after_an_unexpected_exception(tmp_path):
    c = Controller(SessionStore(tmp_path))

    def boom(source):
        raise RuntimeError("bug")
    c._obd_transport = boom
    with pytest.raises(RuntimeError):
        c.read_obd("healthy")
    del c._obd_transport
    assert c.read_obd("healthy")[1] is None


def test_a_real_read_into_a_simulated_case_is_refused_before_the_adapter_is_touched(tmp_path, monkeypatch):
    c = Controller(SessionStore(tmp_path))
    c.read_obd("healthy")
    fake = _serial_via_sim(monkeypatch)
    r, err = c.read_obd(("serial", "/dev/cu.fake"))
    assert r is None and "already has simulated OBD reads" in err and "real adapter" in err
    assert fake.written == [] and [e.type for e in c.case().events()].count("code_read") == 1


def test_a_simulated_read_into_a_real_case_is_refused(tmp_path, monkeypatch):
    _serial_via_sim(monkeypatch)
    c = Controller(SessionStore(tmp_path))
    assert c.read_obd(("serial", "/dev/cu.fake"))[1] is None
    r, err = c.read_obd("healthy")
    assert r is None and "already has real OBD reads" in err and "simulator" in err
    assert [e.type for e in c.case().events()].count("code_read") == 1


def test_reads_of_the_same_kind_accumulate_and_the_lock_is_free_after_a_refusal(tmp_path, monkeypatch):
    c = Controller(SessionStore(tmp_path))
    assert c.read_obd("healthy")[1] is None
    _serial_via_sim(monkeypatch)
    assert c.read_obd(("serial", "/dev/cu.fake"))[1] is not None          # refused
    assert c.read_obd("p2119_zero_rpm")[1] is None                         # the lock was released
    assert [e.type for e in c.case().events()].count("code_read") == 2


def test_a_case_that_already_mixes_both_kinds_refuses_both(tmp_path, monkeypatch):
    c = Controller(SessionStore(tmp_path))
    c.case().append("code_read", "", {"source": "obd", "sim": True, "codes": []})
    c.case().append("code_read", "", {"source": "obd", "sim": False, "codes": []})
    assert c.case().obd_kinds() == {"real", "simulated"}
    _serial_via_sim(monkeypatch)
    assert c.read_obd("healthy")[0] is None
    assert c.read_obd(("serial", "/dev/cu.fake"))[0] is None
    assert [e.type for e in c.case().events()].count("code_read") == 2


def test_an_empty_case_accepts_either_kind(tmp_path):
    assert Controller(SessionStore(tmp_path)).case().obd_kinds() == set()


class Gated(SimTransport):
    """A simulated adapter whose replies wait for `gate` (or a cancel) once `after` commands have been sent."""

    def __init__(self, scenario, gate, after=0):
        super().__init__(scenario)
        self.gate, self.after, self.entered = gate, after, threading.Event()

    def send(self, cmd, timeout=5.0):
        if self.n >= self.after:
            self.entered.set()
            while not self.gate.is_set():
                self.check_cancel()
                time.sleep(0.01)
        return super().send(cmd, timeout)


def test_start_obd_runs_the_read_in_the_background_and_records_it(tmp_path):
    c = Controller(SessionStore(tmp_path))
    assert c.start_obd("p2119_zero_rpm") is None
    c.obd_job.join(10)
    assert c.obd_job.state == "done" and c.obd_job.readout.codes[0]["code"] == "P2119"
    assert [e.type for e in c.case().events()].count("code_read") == 1


def test_a_second_start_while_a_read_runs_is_refused(tmp_path):
    c = Controller(SessionStore(tmp_path))
    assert c.start_obd("slow_adapter") is None
    assert c.start_obd("healthy") == "An OBD read is already running."
    first = c.obd_job
    c.cancel_obd(); first.join(5)
    assert c.obd_job is first


def test_cancel_straight_after_start_records_nothing_and_frees_the_lock(tmp_path):
    c = Controller(SessionStore(tmp_path))
    assert c.start_obd("slow_adapter") is None
    c.cancel_obd()
    c.obd_job.join(5)
    assert c.obd_job.state == "cancelled" and c.case().events() == []
    assert c.start_obd("healthy") is None                  # the next read reopens cleanly
    c.obd_job.join(10)
    assert c.obd_job.state == "done"


def test_stop_after_the_pcm_answered_keeps_what_was_read(tmp_path):
    c = Controller(SessionStore(tmp_path))
    gate = threading.Event()
    t = Gated("many_codes", gate, after=11)               # 6 init + 4 id + 0100 answered, then it waits
    c._obd_transport = lambda source: t
    assert c.start_obd("x") is None
    assert t.entered.wait(5)
    c.stop_obd()
    gate.set()
    c.obd_job.join(10)
    assert c.obd_job.state == "stopped" and c.obd_job.readout.stopped
    events = [e for e in c.case().events() if e.type == "code_read"]
    assert len(events) == 1 and events[0].data["stopped"] is True


def test_stop_before_the_pcm_answers_acts_as_a_cancel(tmp_path):
    c = Controller(SessionStore(tmp_path))
    t = Gated("healthy", threading.Event(), after=0)       # never answers
    c._obd_transport = lambda source: t
    assert c.start_obd("x") is None
    assert t.entered.wait(5)
    c.stop_obd()
    c.obd_job.join(5)
    assert c.obd_job.state == "cancelled" and c.case().events() == []


def test_the_read_goes_to_the_case_it_was_started_on(tmp_path):
    c = Controller(SessionStore(tmp_path))
    ev = threading.Event()
    c._obd_transport = lambda source: Gated("healthy", ev)
    assert c.start_obd("x") is None
    c.session_name = "other"
    ev.set()
    c.obd_job.join(10)
    assert c.obd_job.state == "done"
    assert [e.type for e in Case(c.store.open("default")).events()].count("code_read") == 1
    assert Case(c.store.open("other")).events() == []


def test_start_obd_refuses_the_other_kind_at_once_and_leaves_the_lock_free(tmp_path):
    c = Controller(SessionStore(tmp_path))
    r, err = c.read_obd("healthy")                         # a simulated read is in the case
    assert err is None

    class RealLike(SimTransport):
        sim = False
    c._obd_transport = lambda source: RealLike("healthy")
    msg = c.start_obd("x")
    assert msg and "already has simulated" in msg
    c._obd_transport = lambda source: SimTransport("healthy")
    assert c.start_obd("x") is None
    c.obd_job.join(10)


def test_start_obd_returns_a_transport_failure_and_leaves_the_lock_free(tmp_path, monkeypatch):
    c = Controller(SessionStore(tmp_path))

    def no_port(value):
        raise ObdNoAdapter("No OBD adapter found.")
    monkeypatch.setattr("crank.controller.resolve_port", no_port)
    assert c.start_obd(("serial", "auto")) == "No OBD adapter found."
    assert c.start_obd("healthy") is None
    c.obd_job.join(10)


def test_a_failed_read_is_an_error_job_that_records_nothing(tmp_path):
    c = Controller(SessionStore(tmp_path))
    assert c.start_obd("silent_adapter") is None
    c.obd_job.join(10)
    assert c.obd_job.state == "error" and c.obd_job.message and c.case().events() == []


DROPPED = ("The adapter connection dropped. Press Relink, or Disconnect to read without a standing connection.")


def conn_controller(tmp_path, **kw):
    return Controller(SessionStore(tmp_path / "sessions"), heartbeat_s=kw.pop("heartbeat_s", 0.02), **kw)


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_connect_status_and_disconnect(tmp_path):
    c = conn_controller(tmp_path)
    assert c.connection_status()["state"] == "disconnected"
    assert c.connect("healthy") is None
    s = c.connection_status()
    assert s["state"] == "connected" and s["kind"] == "simulated" and s["reading"] is False
    c.disconnect()
    assert c.connection_status()["state"] == "disconnected"


def test_connect_refuses_the_other_kind_the_case_already_has(tmp_path):
    c = conn_controller(tmp_path)
    c.read_obd("healthy")                                  # a simulated read is in the case

    class RealLike(SimTransport):
        sim = False
    c._obd_transport = lambda source: RealLike("healthy")
    msg = c.connect("x")
    assert msg and "already has simulated" in msg and c.connection_status()["state"] == "disconnected"


def test_connect_returns_a_transport_failure(tmp_path, monkeypatch):
    c = conn_controller(tmp_path)

    def no_port(value):
        raise ObdNoAdapter("No OBD adapter found.")
    monkeypatch.setattr("crank.controller.resolve_port", no_port)
    assert c.connect(("serial", "auto")) == "No OBD adapter found."


def test_two_connects_connect_once(tmp_path):
    c = conn_controller(tmp_path)
    results = []
    ts = [threading.Thread(target=lambda: results.append(c.connect("healthy"))) for _ in range(2)]
    [t.start() for t in ts]; [t.join(5) for t in ts]
    assert sorted(r is None for r in results) == [False, True]
    c.disconnect()


def test_a_read_on_the_open_connection_is_warm_and_keeps_the_port_open(tmp_path):
    c = conn_controller(tmp_path, heartbeat_s=60)
    c.connect("p2119_zero_rpm")
    sent = []
    inner = c.connection._inner.send
    c.connection._inner.send = lambda cmd, timeout=5.0: (sent.append(cmd), inner(cmd, timeout))[1]
    r, err = c.read_obd("ignored")
    assert err is None and r.codes[0]["code"] == "P2119" and "ATZ" not in sent and "ATI" not in sent
    assert c.connection_status()["state"] == "connected" and c.connection.vehicle == "answered"
    assert [e.type for e in c.case().events()].count("code_read") == 1
    c.disconnect()


def test_a_cancelled_read_keeps_the_port_open_and_the_next_read_is_cold(tmp_path):
    c = conn_controller(tmp_path, heartbeat_s=60)
    c.connect("healthy")
    inner = c.connection._inner
    gate = threading.Event()
    real_send = inner.send

    def slow(cmd, timeout=5.0):
        if cmd == "0100":
            while not gate.is_set():
                inner.check_cancel()
                time.sleep(0.01)
        return real_send(cmd, timeout)
    inner.send = slow
    assert c.start_obd("x") is None
    c.cancel_obd(); c.obd_job.join(5)
    assert c.obd_job.state == "cancelled" and c.case().events() == []
    assert c.connection_status()["state"] == "connected" and c.connection.cold
    gate.set()
    sent = []
    inner.send = lambda cmd, timeout=5.0: (sent.append(cmd), real_send(cmd, timeout))[1]
    r, err = c.read_obd("x")
    assert err is None and "ATZ" in sent and c.connection.cold is False      # full setup, then warm again
    c.disconnect()


def test_a_silent_vehicle_is_noted_on_the_connection(tmp_path):
    c = conn_controller(tmp_path, heartbeat_s=60)
    c.connect("no_vehicle")
    r, err = c.read_obd("x")
    assert r is None and err and c.connection.vehicle == "no answer" and c.connection.cold
    c.disconnect()


def test_a_dropped_connection_refuses_a_read_with_the_relink_message(tmp_path):
    c = conn_controller(tmp_path)
    c.connect("heartbeat_drop")
    assert wait_for(lambda: c.connection_status()["state"] == "dropped")
    assert c.start_obd("healthy") == DROPPED
    c.disconnect()
    assert c.start_obd("healthy") is None                  # without a connection it reads on its own port
    c.obd_job.join(10)


def test_relink_reconnects_with_the_same_source(tmp_path):
    c = conn_controller(tmp_path)
    c.connect("heartbeat_drop")
    assert wait_for(lambda: c.connection_status()["state"] == "dropped")
    assert c.relink() is None and c.connection_status()["state"] == "connected"
    c.disconnect()


def test_relink_with_nothing_to_relink_says_so(tmp_path):
    assert "Press Connect" in conn_controller(tmp_path).relink()


def test_a_read_asked_for_the_connection_after_it_closed_is_refused(tmp_path):
    c = conn_controller(tmp_path)
    msg = c.start_obd("connection")
    assert msg and "Press Connect" in msg


def test_connect_and_disconnect_are_refused_while_a_read_runs(tmp_path):
    c = conn_controller(tmp_path)
    assert c.start_obd("slow_adapter") is None
    assert "read is running" in c.connect("healthy") and c.connection_status()["reading"] is True
    c.cancel_obd(); c.obd_job.join(5)


def test_a_read_waits_for_a_heartbeat_in_flight(tmp_path):
    c = conn_controller(tmp_path, heartbeat_s=0.01)
    c.connect("healthy")
    release = threading.Event()
    inner = c.connection._inner.send

    def slow_atrv(cmd, timeout=5.0):
        if cmd == "ATRV":
            release.wait(2)
        return inner(cmd, timeout)
    c.connection._inner.send = slow_atrv
    assert wait_for(lambda: c.connection.pinging)
    threading.Timer(0.2, release.set).start()
    assert c.start_obd("x") is None                        # waited for the ping instead of refusing
    c.obd_job.join(10)
    assert c.obd_job.state == "done"
    c.disconnect()


def test_disconnect_waits_for_a_heartbeat_in_flight(tmp_path):
    c = conn_controller(tmp_path, heartbeat_s=0.01)
    c.connect("healthy")
    release = threading.Event()
    inner = c.connection._inner.send

    def slow_atrv(cmd, timeout=5.0):
        if cmd == "ATRV":
            release.wait(2)
        return inner(cmd, timeout)
    c.connection._inner.send = slow_atrv
    assert wait_for(lambda: c.connection.pinging)
    threading.Timer(0.2, release.set).start()
    c.disconnect()
    assert c.connection_status()["state"] == "disconnected"


def test_every_read_leaves_a_trace_in_the_log_even_when_cancelled(tmp_path):
    c = conn_controller(tmp_path)
    assert c.start_obd("slow_adapter") is None
    time.sleep(0.3)
    c.cancel_obd(); c.obd_job.join(5)
    text = c.traffic.path.read_text()
    assert "read started" in text and "read cancelled" in text and ">> ATZ" in text


def test_a_log_that_cannot_be_written_does_not_break_a_read(tmp_path):
    blocked = tmp_path / "logs"
    blocked.write_text("not a directory")
    c = Controller(SessionStore(tmp_path / "sessions"), logs_dir=blocked)
    r, err = c.read_obd("healthy")
    assert err is None and r.live
    assert c.connection_status()["traffic_error"]


def test_delete_session_is_refused_while_a_read_runs_on_it_but_not_on_another(tmp_path):
    c = Controller(SessionStore(tmp_path))
    ev = threading.Event()
    c._obd_transport = lambda source: Gated("healthy", ev)
    c.store.open("other")
    assert c.start_obd("x") is None                         # runs on "default"
    assert c.delete_session("default") is not None and "default" in c.store.list_sessions()
    assert c.delete_session("other") is None
    ev.set()
    c.obd_job.join(10)
