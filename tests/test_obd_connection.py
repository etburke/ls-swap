import threading
import time

import pytest

from crank.obd.connection import ObdConnection
from crank.obd.trafficlog import TrafficLog
from crank.obd.transport import SimTransport


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def conn():
    c = ObdConnection(heartbeat_s=0.02)
    yield c
    c.disconnect()


def test_connect_brings_the_adapter_up_without_asking_the_vehicle(conn):
    t = SimTransport("healthy")
    sent = []
    inner = t.send
    t.send = lambda cmd, timeout=5.0: (sent.append(cmd), inner(cmd, timeout))[1]
    assert conn.connect(t, kind="simulated", source="healthy") is None
    assert conn.state == "connected" and conn.kind == "simulated" and conn.source == "healthy"
    assert "STN2255" in conn.adapter.label() and conn.voltage == 12.4 and conn.vehicle == "not checked"
    assert not any(c.startswith("01") for c in sent) and "ATRV" in sent and conn.cold is False


def test_status_is_a_plain_dict(conn):
    conn.connect(SimTransport("healthy"), kind="simulated")
    s = conn.status()
    assert s["state"] == "connected" and "STN2255" in s["adapter"] and s["voltage"] == 12.4
    assert s["vehicle"] == "not checked" and s["last_heard_age"] >= 0 and s["kind"] == "simulated"


def test_connect_failure_leaves_an_error_and_the_port_closed(conn):
    t = SimTransport("silent_adapter")
    closed = []
    t.close = lambda: closed.append(1)
    msg = conn.connect(t)
    assert msg and conn.state == "error" and conn.message == msg and closed and conn.transport is None


def test_connect_when_the_port_cannot_open_reports_it(conn):
    t = SimTransport("healthy"); t.sc.fault = "no_adapter"
    assert "No OBD adapter" in conn.connect(t) and conn.state == "error"


def test_a_second_connect_is_refused(conn):
    conn.connect(SimTransport("healthy"))
    assert conn.connect(SimTransport("healthy")) == "Already connected."


def test_disconnect_closes_the_port_and_stops_the_heartbeat(conn):
    t = SimTransport("healthy")
    closed = []
    t.close = lambda: closed.append(1)
    conn.connect(t)
    conn.disconnect()
    assert conn.state == "disconnected" and closed and conn.transport is None
    n = t.atrv_n
    time.sleep(0.15)
    assert t.atrv_n == n


def test_the_heartbeat_updates_the_voltage_and_last_heard(conn):
    conn.connect(SimTransport("healthy"))
    first = conn.last_heard
    assert wait_for(lambda: conn.last_heard > first)


def test_two_missed_heartbeats_drop_the_connection_and_close_the_port():
    c = ObdConnection(heartbeat_s=0.02)
    t = SimTransport("heartbeat_drop")
    closed = []
    inner = t.close
    t.close = lambda: (closed.append(1), inner())
    c.connect(t)
    assert wait_for(lambda: c.state == "dropped") and closed and "heartbeat" in c.message.lower()
    c.disconnect()


def test_a_dropped_connection_can_connect_again():
    c = ObdConnection(heartbeat_s=0.02)
    c.connect(SimTransport("heartbeat_drop"))
    assert wait_for(lambda: c.state == "dropped")
    assert c.connect(SimTransport("healthy")) is None and c.state == "connected"
    c.disconnect()


def test_relink_with_the_adapter_still_gone_reports_an_error():
    c = ObdConnection(heartbeat_s=0.02)
    c.connect(SimTransport("heartbeat_drop"))
    assert wait_for(lambda: c.state == "dropped")
    gone = SimTransport("healthy"); gone.sc.fault = "no_adapter"
    start = time.monotonic()
    assert c.connect(gone) and c.state == "error" and time.monotonic() - start < 2
    c.disconnect()


def test_the_heartbeat_does_not_run_while_a_read_holds_the_lock():
    lock = threading.Lock()
    c = ObdConnection(heartbeat_s=0.02, read_lock=lock)
    t = SimTransport("heartbeat_drop")
    lock.acquire()                                            # a read is running before and while we connect
    c.connect(t)
    time.sleep(0.3)
    assert c.state == "connected" and t.atrv_n <= 1           # no pings, so no misses
    lock.release()
    assert wait_for(lambda: c.state == "dropped")
    c.disconnect()


def test_an_adapter_reset_in_a_heartbeat_marks_the_connection_cold(conn):
    t = SimTransport("healthy")
    conn.connect(t)
    inner = t.send
    t.send = lambda cmd, timeout=5.0: "LV RESET" if cmd == "ATRV" else inner(cmd, timeout)
    assert wait_for(lambda: conn.cold)


def test_heartbeats_are_logged_with_the_hb_tag(tmp_path):
    log = TrafficLog(tmp_path / "logs")
    c = ObdConnection(log=log, heartbeat_s=0.02)
    c.connect(SimTransport("healthy"))
    assert wait_for(lambda: any(" hb >> ATRV" in l for l in log.path.read_text().splitlines()))
    c.disconnect()
    text = log.path.read_text()
    assert "connected" in text and "disconnected" in text


def test_note_vehicle_and_cold_and_adopt(conn):
    conn.connect(SimTransport("healthy"))
    conn.note_vehicle(False)
    assert conn.vehicle == "no answer"
    conn.note_vehicle(True)
    assert conn.vehicle == "answered" and conn.status()["vehicle_age"] >= 0
    conn.mark_cold()
    assert conn.cold
    readout = type("R", (), {"adapter": conn.adapter})()
    conn.adopt(readout)
    assert conn.cold is False and conn.vehicle == "answered"
