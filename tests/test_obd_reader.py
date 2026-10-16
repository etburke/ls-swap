import pytest

from crank.obd.errors import ObdAdapterError, ObdNoAdapter, ObdProtocolError, ObdTimeout
from crank.obd.pids import LIVE_PIDS
from crank.obd.reader import ObdReader
from crank.obd.transport import LIVE_RUNNING, SCENARIOS, SimTransport


def read(name, **kw):
    return ObdReader(SimTransport(name), retry_pause=0, **kw).read()


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
    assert "7E8" in r.raw["03"] and "010C" in r.raw


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
                return "7E8 05 42 02 00 00 00 00 00"
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


def test_raw_is_reset_between_reads():
    reader = ObdReader(SimTransport("healthy"))
    reader.read()
    first = set(reader.raw)
    reader.t.n = 0
    r2 = reader.read()
    assert set(r2.raw) == first and r2.raw is reader.raw


def test_close_failure_does_not_mask_the_original_error():
    t = SimTransport("drops_mid_read")

    def boom():
        raise OSError("port gone")
    t.close = boom
    with pytest.raises(ObdTimeout):
        ObdReader(t).read()


def test_close_failure_after_a_good_read_is_reported():
    t = SimTransport("healthy")

    def boom():
        raise OSError("port gone")
    t.close = boom
    with pytest.raises(OSError):
        ObdReader(t).read()


def test_dtc_count_and_unread_modes_in_the_code_line():
    assert read("many_codes").dtc_count == 5
    r = read("limited_pcm")
    r.codes = []
    assert r.lines()[0] == "Codes (simulated): no codes reported by the modes that were read"
    assert read("healthy").lines()[0] == "Codes (simulated): none stored, pending or permanent"


def test_scenarios_are_not_shared_between_transports():
    a = SimTransport("p2119_zero_rpm")
    a.sc.stored.append("P0000")
    assert SCENARIOS["p2119_zero_rpm"].stored == ["P2119"]
    assert SimTransport("p2119_zero_rpm").sc.stored == ["P2119"]


def test_no_data_on_the_first_request_asks_about_the_key():
    class KeyOff(SimTransport):
        def _obd(self, cmd):
            return "NO DATA"
    with pytest.raises(ObdProtocolError, match="Is the key on"):
        ObdReader(KeyOff("healthy")).read()


def test_pcm_and_trans_records_both_ecus_codes_tagged():
    r = read("pcm_and_trans")
    assert {(c["code"], c["ecu"]) for c in r.codes} == {("P2119", "7E8"), ("P0700", "7E9")}
    assert r.ecus == ["7E8", "7E9"] and r.primary_ecu == "7E8" and r.notes == []
    assert r.freeze_frame["code"] == "P2119" and live(r)["Engine RPM"].value == 0.0
    assert "P0700 (stored, 7E9)" in " ".join(r.lines())


def test_interleaved_multiframe_code_lists_are_attributed():
    r = read("interleaved_multiframe")
    assert {c["code"] for c in r.codes if c["ecu"] == "7E8"} == {"P0335", "P0336", "P0016"}
    assert {c["code"] for c in r.codes if c["ecu"] == "7E9"} == {"P0700", "P0701", "P0702"}


def test_same_code_from_two_ecus_is_two_rows():
    r = read("same_code_two_ecus")
    assert sorted((c["code"], c["ecu"]) for c in r.codes) == [("P0335", "7E8"), ("P0335", "7E9")]


def test_pcm_not_on_7e8_is_used_and_noted():
    r = read("pcm_not_7e8")
    assert r.primary_ecu == "7E9" and r.codes[0]["ecu"] == "7E9"
    assert any("7E9" in n for n in r.notes) and live(r)["Engine RPM"].supported


def test_an_ecu_rejecting_a_mode_does_not_fail_the_read():
    r = read("trans_rejects_mode")
    assert "0A (7E9)" in r.unsupported_modes and "0A" not in r.unsupported_modes
    assert {c["code"] for c in r.codes} == {"P2119", "P0700"}


def test_single_ecu_read_looks_exactly_as_before():
    r = read("p2119_zero_rpm")
    assert r.ecus == ["7E8"] and r.codes[0]["ecu"] == "7E8"
    assert not any("7E8" in line for line in r.lines())
    assert r.lines()[0] == "Codes (simulated): P2119 (stored)"


def test_slow_protocol_search_succeeds_with_the_long_first_timeout():
    assert live(read("slow_search"))["Engine RPM"].value == 750.0


def test_slow_protocol_search_times_out_if_the_first_timeout_is_short():
    with pytest.raises(ObdTimeout):
        read("slow_search", first_timeout=5.0)


def test_only_the_first_request_gets_the_long_timeout():
    seen = []

    class Rec(SimTransport):
        def send(self, cmd, timeout=5.0):
            seen.append((cmd, timeout))
            return super().send(cmd, timeout)
    ObdReader(Rec("healthy"), retry_pause=0).read()
    assert dict(seen)["0100"] == 20.0
    assert {t for c, t in seen if c != "0100"} == {5.0}


def test_a_clean_read_has_no_notes():
    assert read("healthy").notes == [] and read("p2119_zero_rpm").notes == []


def test_a_transient_error_is_retried_once_and_noted():
    r = read("transient_can_error")
    assert live(r)["Engine RPM"].value == 750.0
    assert r.notes == ["Retried 010C: the adapter reported CAN ERROR."]
    assert "Retried 010C: the adapter reported CAN ERROR." in r.lines()


def test_a_persistent_transient_error_fails_with_the_specific_message():
    with pytest.raises(ObdAdapterError, match="CAN error"):
        read("persistent_can_error")


def test_a_fatal_adapter_error_is_not_retried():
    with pytest.raises(ObdAdapterError, match="buffer overflowed"):
        read("buffer_full")                # error_times=1: a retry would have succeeded


def test_lv_reset_reinitialises_the_adapter_and_the_read_continues():
    sent = []

    class Rec(SimTransport):
        def send(self, cmd, timeout=5.0):
            sent.append(cmd)
            return super().send(cmd, timeout)
    r = ObdReader(Rec("lv_reset_mid_read"), retry_pause=0).read()
    assert live(r)["Engine RPM"].value == 750.0 and r.codes == []
    assert r.notes[0].startswith("The adapter reset (LV RESET) during 010C")
    assert sent.count("ATZ") == 2 and sent.count("ATH1") == 2


def test_lv_reset_retry_uses_the_long_timeout_again():
    seen = []

    class Rec(SimTransport):
        def send(self, cmd, timeout=5.0):
            seen.append((cmd, timeout))
            return super().send(cmd, timeout)
    ObdReader(Rec("lv_reset_mid_read"), retry_pause=0).read()
    assert [t for c, t in seen if c == "010C"] == [5.0, 20.0]


def test_a_second_lv_reset_after_the_retry_is_raised_not_looped():
    t = SimTransport("lv_reset_mid_read")
    t.errors_left = 99                                       # the adapter keeps resetting
    with pytest.raises(ObdAdapterError, match="pin 16"):
        ObdReader(t, retry_pause=0).read()
    assert t.errors_left == 97                               # two attempts, no more


def test_a_failure_while_reinitialising_after_a_reset_is_raised():
    class Dies(SimTransport):
        def send(self, cmd, timeout=5.0):
            if cmd == "ATZ" and self.errors_left == 0:       # the re-init after the reset
                raise ObdTimeout("The adapter stopped answering.")
            return super().send(cmd, timeout)
    with pytest.raises(ObdTimeout):
        ObdReader(Dies("lv_reset_mid_read"), retry_pause=0).read()


def test_a_reply_with_data_and_an_adapter_error_is_retried():
    class Mixed(SimTransport):
        def __init__(self):
            super().__init__("healthy")
            self.first = True

        def _obd(self, cmd):
            out = super()._obd(cmd)
            if cmd == "010C" and self.first:
                self.first = False
                return out + "\rCAN ERROR"
            return out
    r = ObdReader(Mixed(), retry_pause=0).read()
    assert live(r)["Engine RPM"].value == 750.0 and len(r.notes) == 1


def test_retries_are_capped_per_read():
    pids = [f"01{p:02X}" for p in LIVE_PIDS if p in LIVE_RUNNING][:4]

    class Flaky(SimTransport):
        def __init__(self):
            super().__init__("healthy")
            self.seen = set()

        def _obd(self, cmd):
            if cmd in pids and cmd not in self.seen:
                self.seen.add(cmd)
                return "CAN ERROR"
            return super()._obd(cmd)
    with pytest.raises(ObdAdapterError):
        ObdReader(Flaky(), retry_pause=0).read()


def test_a_first_request_that_times_out_even_at_the_long_timeout_fails_clearly():
    with pytest.raises(ObdTimeout, match="did not answer"):
        read("slow_search", first_timeout=1.0)
