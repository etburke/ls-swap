import pytest

from crank.obd.errors import ObdNoAdapter, ObdProtocolError, ObdTimeout
from crank.obd.reader import ObdReader
from crank.obd.transport import SCENARIOS, SimTransport


def read(name):
    return ObdReader(SimTransport(name)).read()


def live(r):
    return {v.name: v for v in r.live}


def test_healthy_read_has_no_codes_and_live_values():
    r = read("healthy")
    assert r.codes == [] and r.mil_on is False and r.sim is True and r.freeze_frame is None
    assert live(r)["Engine RPM"].value == 750.0
    assert live(r)["Control module voltage"].value == pytest.approx(14.0)
    assert r.protocol == "A6"


def test_stored_code_with_description_mil_and_freeze_frame():
    r = read("p2119_zero_rpm")
    assert [(c["code"], c["status"]) for c in r.codes] == [("P2119", "stored")]
    assert "Throttle" in r.codes[0]["description"] and r.mil_on is True
    assert r.freeze_frame["code"] == "P2119"
    assert {"name": "Engine RPM", "value": 0.0, "unit": "rpm"} in r.freeze_frame["values"]
    assert live(r)["Engine RPM"].value == 0.0                 # a real zero is kept as a zero


def test_codes_carry_their_status_and_multi_frame_replies_work():
    r = read("many_codes")
    got = {(c["code"], c["status"]) for c in r.codes}
    assert got == {("P0335", "stored"), ("P0336", "stored"), ("P0016", "stored"),
                   ("P0340", "pending"), ("P0335", "permanent")}


def test_unsupported_pids_are_unsupported_not_zero():
    r = read("limited_pcm")
    v = live(r)
    assert v["Engine RPM"].supported and not v["Control module voltage"].supported
    assert v["Control module voltage"].value is None and not v["Vehicle speed"].supported


def test_unsupported_modes_are_listed_and_do_not_fail_the_read():
    r = read("limited_pcm")
    assert "02" in r.unsupported_modes and r.freeze_frame is None
    assert [c["code"] for c in r.codes] == ["P2119"]


def test_raw_responses_are_kept_as_evidence():
    r = read("p2119_zero_rpm")
    assert r.raw["03"].startswith("43") and "010C" in r.raw


@pytest.mark.parametrize("name,exc", [("silent_adapter", ObdTimeout), ("no_vehicle", ObdProtocolError),
                                       ("garbage", ObdProtocolError), ("drops_mid_read", ObdTimeout)])
def test_failures_raise_clear_errors(name, exc):
    with pytest.raises(exc):
        read(name)


def test_vehicle_not_answering_mentions_the_key():
    with pytest.raises(ObdProtocolError, match="UNABLE|Unable|did not answer"):
        read("no_vehicle")


def test_transport_is_closed_even_when_the_read_fails():
    t = SimTransport("drops_mid_read")
    closed = []
    t.close = lambda: closed.append(True)
    with pytest.raises(ObdTimeout):
        ObdReader(t).read()
    assert closed


def test_a_missing_adapter_raises_no_adapter():
    sim = SimTransport("healthy")
    sim.sc = type(sim.sc)(fault="no_adapter")
    with pytest.raises(ObdNoAdapter):
        ObdReader(sim).read()


def test_every_scenario_is_known_and_unknown_is_rejected():
    assert {"healthy", "p2119_zero_rpm", "silent_adapter"} <= set(SCENARIOS)
    with pytest.raises(ValueError):
        SimTransport("nope")


def test_readout_lines_are_plain_language():
    lines = read("limited_pcm").lines()
    assert lines[0] == "Codes (simulated): P2119 (stored)" and "Check engine light: on" in lines
    assert any(l.startswith("Live values read: 2 of 10 (not supported:") for l in lines)
    assert any("does not support mode" in l for l in lines)


def test_freeze_frame_dtc_0000_means_no_freeze_frame():
    class NoFreeze(SimTransport):
        def send(self, cmd, timeout=5.0):
            if cmd.strip().upper().replace(" ", "") == "020200":
                return "42 02 00 00 00"
            return super().send(cmd, timeout)
    r = ObdReader(NoFreeze("p2119_zero_rpm")).read()
    assert r.freeze_frame is None
    assert not any(k.startswith("02") and k != "020200" for k in r.raw)


def test_busy_pcm_on_mode_03_fails_the_read_rather_than_marking_it_unsupported():
    class Busy(SimTransport):
        def send(self, cmd, timeout=5.0):
            if cmd.strip() == "03":
                return "7F 03 21"
            return super().send(cmd, timeout)
    with pytest.raises(ObdProtocolError):
        ObdReader(Busy("healthy")).read()
