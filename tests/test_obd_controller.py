import threading

import pytest

from crank.controller import Controller
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
