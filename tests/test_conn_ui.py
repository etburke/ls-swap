from conn_ui import status_text, vehicle_line


def s(**kw):
    base = {"state": "disconnected", "message": "", "adapter": "", "voltage": None, "voltage_age": None,
            "last_heard_age": None, "vehicle": "not checked", "vehicle_age": None, "kind": "", "source": "",
            "cold": False, "reading": False, "traffic_error": ""}
    base.update(kw)
    return base


def test_status_text_for_each_state():
    assert status_text(s()) == "⚪ Disconnected"
    assert status_text(s(state="connecting")) == "🟠 Connecting…"
    assert status_text(s(state="connected", adapter="OBDLink MX+ (STN2255 v5.12.4)", voltage=12.4,
                         last_heard_age=2.2)) == "🟢 Connected, OBDLink MX+ (STN2255 v5.12.4), 12.4 V, last heard 2 s ago"
    assert status_text(s(state="dropped", message="heartbeat failed")) == "🔴 Dropped: heartbeat failed. Press Relink."
    assert status_text(s(state="error", message="No OBD adapter found.")) == "🔴 No OBD adapter found."


def test_status_text_without_a_voltage():
    assert "V," not in status_text(s(state="connected", adapter="A", last_heard_age=1.0))


def test_status_text_marks_a_simulated_connection():
    assert "(simulated)" in status_text(s(state="connected", adapter="A", last_heard_age=1.0, kind="simulated"))


def test_vehicle_line():
    assert vehicle_line(s()) == "Vehicle: not checked"
    assert vehicle_line(s(vehicle="answered", vehicle_age=30.0)) == "Vehicle: answered 30 s ago"
    assert vehicle_line(s(vehicle="no answer", vehicle_age=5.0)) == "Vehicle: no answer (is the key on?)"
