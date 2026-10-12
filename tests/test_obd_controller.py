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
