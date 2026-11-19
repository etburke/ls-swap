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
