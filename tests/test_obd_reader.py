import threading
import time

import pytest

from crank.obd.adapter import AdapterInfo
from crank.obd.errors import (ObdAdapterError, ObdCancelled, ObdNoAdapter, ObdProtocolError, ObdTimeout,
                              ObdVehicleSilent)
from crank.obd.pids import LIVE_PIDS
from crank.obd.reader import INIT, ObdReader
from crank.obd.transport import LIVE_RUNNING, SCENARIOS, HeldTransport, SimTransport


def read(name, **kw):
    return ObdReader(SimTransport(name), retry_pause=0, **kw).read()


def live(r):
    return {v.name: v for v in r.live}


def sent_by(t):
    """Wrap a SimTransport so every command it is sent is kept in t.sent."""
    t.sent = []
    real = t.send

    def send(cmd, timeout=5.0):
        t.sent.append(cmd)
        return real(cmd, timeout)
    t.send = send
    return t


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
    assert any(l.startswith("Live values read: 2 of 10 (not in the PCM's support map:") for l in lines)
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


def test_a_genuine_stn_adapter_is_identified_and_recorded():
    r = read("healthy")
    a = r.adapter
    assert a.supports_st and a.elm_version == "ELM327 v1.5" and a.stn_version == "STN2255 v4.3.1"
    assert a.device == "OBDLink MX+ 1234" and a.serial == "110012345678"
    assert r.notes == []                                       # a clean identification leaves no note
    assert r.to_event_data()["adapter"]["stn_version"] == "STN2255 v4.3.1"
    assert r.raw["STI"] == "STN2255 v4.3.1" and r.raw["ATI"] == "ELM327 v1.5"


def test_an_elm327_clone_is_identified_as_plain_and_the_read_succeeds():
    t = sent_by(SimTransport("elm_clone"))
    r = ObdReader(t, retry_pause=0).read()
    assert not r.adapter.supports_st and r.adapter.elm_version == "ELM327 v1.5"
    assert r.adapter.device == "" and r.adapter.serial == "" and r.notes == []
    assert "STDI" not in t.sent and "STSN" not in t.sent       # no ST command beyond the one that proved it
    assert "not an STN adapter" in r.adapter.label()
    assert live(r)["Engine RPM"].value == 750.0


def test_a_clone_that_answers_sti_with_junk_is_not_stn_and_draws_no_note():
    class Junk(SimTransport):
        def send(self, cmd, timeout=5.0):
            return "@#$%^ \r\x00\x01" if cmd == "STI" else super().send(cmd, timeout)

    r = ObdReader(Junk("healthy"), retry_pause=0).read()
    assert not r.adapter.supports_st and r.notes == []


def test_a_timeout_on_one_id_command_is_noted_and_the_read_completes():
    r = read("flaky_adapter_id")
    assert r.adapter.supports_st and r.adapter.device == "OBDLink MX+ 1234" and r.adapter.serial == ""
    assert any("STSN" in n and "timed out" in n for n in r.notes)
    assert r.adapter.notes and r.adapter.notes[0] in r.notes
    assert live(r)["Engine RPM"].value == 750.0


def test_an_adapter_error_on_an_id_command_is_noted_not_fatal():
    class Busy(SimTransport):
        def send(self, cmd, timeout=5.0):
            return "BUS BUSY" if cmd == "STDI" else super().send(cmd, timeout)

    r = ObdReader(Busy("healthy"), retry_pause=0).read()
    assert r.adapter.device == "" and r.adapter.serial == "110012345678"
    assert any("STDI" in n and "BUS BUSY" in n for n in r.notes)


def test_a_missing_adapter_during_identification_still_fails_the_read():
    class Gone(SimTransport):
        def send(self, cmd, timeout=5.0):
            if cmd == "STI":
                raise ObdNoAdapter("The adapter connection dropped.")
            return super().send(cmd, timeout)

    with pytest.raises(ObdNoAdapter):
        ObdReader(Gone("healthy"), retry_pause=0).read()


def test_identification_runs_once_after_init_and_before_the_first_request():
    t = sent_by(SimTransport("lv_reset_mid_read"))             # an LV RESET re-runs INIT mid-read
    ObdReader(t, retry_pause=0).read()
    assert t.sent.count("STI") == 1 and t.sent.count("ATI") == 1
    assert t.sent.index("ATI") > t.sent.index("ATSP0") and t.sent.index("STI") < t.sent.index("0100")


def test_result_lines_show_the_adapter_second():
    lines = read("healthy").lines()
    assert lines[1] == "Adapter (simulated): OBDLink MX+ 1234 (STN2255 v4.3.1, serial 110012345678)"
    assert read("elm_clone").lines()[1].startswith("Adapter (simulated): ELM327 v1.5 (not an STN adapter")


def test_a_refused_pid_is_marked_and_the_read_completes():
    r = read("pid_refused")
    v = live(r)
    assert v["Vehicle speed"].reason == "refused" and v["Vehicle speed"].value is None
    assert v["Engine RPM"].value == 750.0 and v["Engine RPM"].reason == "" and r.notes == []


def test_busy_once_is_retried_noted_and_then_read():
    r = read("pid_busy_once")
    assert live(r)["Engine RPM"].value == 750.0 and live(r)["Engine RPM"].reason == ""
    assert r.notes == ["Retried 010C: the PCM reported busy (NRC 22)."]


def test_busy_always_ends_busy_after_one_retry():
    r = read("pid_busy_always")
    v = live(r)["Engine RPM"]
    assert v.reason == "busy" and v.value is None
    assert r.notes == ["Retried 010C: the PCM reported busy (NRC 22)."]
    assert live(r)["Coolant temperature"].value == 90


def test_the_retry_cap_holds_across_many_busy_pids():
    r = read("many_pids_busy")
    assert len([n for n in r.notes if n.startswith("Retried")]) == 3
    busy = ["Engine RPM", "Coolant temperature", "Vehicle speed", "Engine load", "Short term fuel trim bank 1"]
    assert {live(r)[n].reason for n in busy} == {"busy"}
    assert live(r)["Throttle position"].value is not None


def test_no_data_on_a_listed_pid_is_no_answer_not_unsupported():
    assert live(read("pid_no_answer"))["Vehicle speed"].reason == "no_answer"


def test_pids_the_map_does_not_list_are_not_in_map():
    assert live(read("limited_pcm"))["Control module voltage"].reason == "not_in_map"


def test_a_refused_status_request_leaves_the_light_unknown_and_notes_it():
    r = read("status_refused")
    assert r.mil_on is None and live(r)["Engine RPM"].value == 750.0
    assert any("0101" in n and "refused by the PCM" in n for n in r.notes)


def test_a_refused_support_map_tries_the_pids_in_its_range_anyway():
    r = read("map_refused")
    assert live(r)["Control module voltage"].value == pytest.approx(14.0)      # PID 0x42 is past the map
    assert any("support map 20" in n and "tried anyway" in n for n in r.notes)
    assert all(v.reason != "not_in_map" for v in r.live if v.pid > 0x20)


def test_the_pcm_refusing_the_first_request_fails_with_a_clear_message():
    with pytest.raises(ObdProtocolError, match="refused the supported-PID request.*11"):
        read("pcm_refuses_0100")


def test_another_ecu_busy_on_a_pid_does_not_fail_or_retry_the_read():
    r = read("trans_busy_on_pid")
    assert live(r)["Engine RPM"].value == 750.0 and r.notes == []


def test_freeze_frame_values_that_fail_are_listed_with_a_reason():
    r = read("p2119_zero_rpm")
    miss = {u["name"]: u["reason"] for u in r.freeze_frame["unavailable"]}
    assert miss["Vehicle speed"] == "no_answer" and "Engine RPM" not in miss

    class FreezeBusy(SimTransport):
        def send(self, cmd, timeout=5.0):
            return "7E8 03 7F 02 22" if cmd.strip() == "020500" else super().send(cmd, timeout)

    r = ObdReader(FreezeBusy("p2119_zero_rpm"), retry_pause=0).read()
    miss = {u["name"]: u["reason"] for u in r.freeze_frame["unavailable"]}
    assert miss["Coolant temperature"] == "busy"
    assert {"name": "Engine RPM", "value": 0.0, "unit": "rpm"} in r.freeze_frame["values"]


def test_a_busy_freeze_frame_request_is_noted_not_fatal():
    class FreezeBusy(SimTransport):
        def send(self, cmd, timeout=5.0):
            return "7E8 03 7F 02 22" if cmd.strip() == "020200" else super().send(cmd, timeout)

    r = ObdReader(FreezeBusy("p2119_zero_rpm"), retry_pause=0).read()
    assert r.freeze_frame is None and any("Freeze frame unavailable" in n for n in r.notes)
    assert [c["code"] for c in r.codes] == ["P2119"]


def test_result_lines_group_the_missing_values_by_reason():
    lines = read("pid_refused").lines()
    assert any(l.startswith("Live values read: 9 of 10 (refused by the PCM: Vehicle speed)") for l in lines)
    assert "(" not in [l for l in read("healthy").lines() if l.startswith("Live values")][0]


def test_event_data_records_every_value_without_a_reason():
    assert read("healthy").to_event_data()["unavailable"] == []
    got = read("pid_refused").to_event_data()["unavailable"]
    assert got == [{"name": "Vehicle speed", "reason": "refused"}]
    assert {"name": "Control module voltage", "reason": "not_in_map"} in read("limited_pcm").to_event_data()["unavailable"]


def test_atdpn_reply_with_an_echo_or_blank_lines_still_parses():
    class Echo(SimTransport):
        def send(self, cmd, timeout=5.0):
            return "ATDPN\r\rA6" if cmd == "ATDPN" else super().send(cmd, timeout)

    assert ObdReader(Echo("healthy"), retry_pause=0).read().protocol == "A6"


def test_an_empty_atdpn_reply_is_a_clear_error():
    class Silent(SimTransport):
        def send(self, cmd, timeout=5.0):
            return "" if cmd == "ATDPN" else super().send(cmd, timeout)

    with pytest.raises(ObdProtocolError, match="did not report its protocol"):
        ObdReader(Silent("healthy"), retry_pause=0).read()


def test_a_cancel_set_before_the_read_connects_to_nothing():
    ev = threading.Event(); ev.set()
    t = sent_by(SimTransport("healthy"))
    with pytest.raises(ObdCancelled):
        ObdReader(t, cancel=ev, retry_pause=0).read()
    assert t.sent == [] and not t.opened


def test_a_cancel_in_the_middle_stops_the_read_and_closes_the_transport():
    ev, closed = threading.Event(), []
    t = sent_by(SimTransport("healthy"))
    inner = t.send

    def send(cmd, timeout=5.0):
        if cmd == "010C":
            ev.set()
        return inner(cmd, timeout)
    t.send, t.close = send, lambda: closed.append(1)
    with pytest.raises(ObdCancelled):
        ObdReader(t, cancel=ev, retry_pause=0).read()
    assert closed and "010C" in t.sent and "0142" not in t.sent      # 0142 is asked right after 010C


def test_a_cancel_ends_the_pause_before_a_retry():
    ev = threading.Event()
    threading.Timer(0.1, ev.set).start()
    start = time.monotonic()
    with pytest.raises(ObdCancelled):
        ObdReader(SimTransport("transient_can_error"), cancel=ev, retry_pause=30).read()
    assert time.monotonic() - start < 5.0


def test_a_reader_without_a_cancel_event_still_reads():
    assert ObdReader(SimTransport("healthy"), retry_pause=0).read().live


def stop_read(scenario, after, **kw):
    """Read `scenario`, pressing Stop as soon as `after` has been answered."""
    stop = threading.Event()
    t = sent_by(SimTransport(scenario))
    inner = t.send

    def send(cmd, timeout=5.0):
        r = inner(cmd, timeout)
        if cmd == after:
            stop.set()
        return r
    t.send = send
    return ObdReader(t, stop=stop, retry_pause=0, **kw).read(), t


def test_stop_before_the_pcm_answers_acts_as_a_cancel():
    stop = threading.Event(); stop.set()
    t = sent_by(SimTransport("healthy"))
    with pytest.raises(ObdCancelled):
        ObdReader(t, stop=stop, retry_pause=0).read()
    assert t.sent == []


def test_stop_during_the_adapter_identification_acts_as_a_cancel():
    stop = threading.Event()
    t = sent_by(SimTransport("healthy"))
    inner = t.send

    def send(cmd, timeout=5.0):
        if cmd == "STI":
            stop.set()
        return inner(cmd, timeout)
    t.send = send
    with pytest.raises(ObdCancelled):
        ObdReader(t, stop=stop, retry_pause=0).read()


def test_cancel_wins_over_stop():
    cancel, stop = threading.Event(), threading.Event()
    cancel.set(); stop.set()
    with pytest.raises(ObdCancelled):
        ObdReader(SimTransport("healthy"), cancel=cancel, stop=stop, retry_pause=0).read()


def test_stop_right_after_the_first_answer_keeps_nothing_but_says_so():
    r, t = stop_read("many_codes", "0100")
    assert r.stopped and r.codes == [] and r.mil_on is None and r.freeze_frame is None
    assert r.not_read == ["03", "07", "0A", "02"]
    assert len(r.live) == len(LIVE_PIDS) and all(v.reason == "stopped" and not v.supported for v in r.live)
    assert "Stopped by you before the read finished." in r.notes
    assert "03" not in t.sent


def test_stop_after_mode_03_keeps_the_stored_codes_and_not_reads_the_rest():
    r, t = stop_read("many_codes", "03")
    assert r.stopped and {c["status"] for c in r.codes} == {"stored"} and len(r.codes) == 3
    assert r.not_read == ["07", "0A", "02"] and "07" not in t.sent


def test_stop_in_the_live_values_keeps_the_values_already_read():
    r, t = stop_read("healthy", "010C")
    assert r.stopped and r.not_read == [] and r.live[0].supported and r.live[0].name == "Engine RPM"
    assert all(v.reason == "stopped" for v in r.live[1:]) and len(r.live) == len(LIVE_PIDS)
    assert "0142" not in t.sent


def test_stop_in_the_freeze_frame_drops_the_partial_frame():
    r, _ = stop_read("p2119_zero_rpm", "020200")
    assert r.stopped and r.freeze_frame is None and r.not_read == ["02"]
    assert [c["code"] for c in r.codes] == ["P2119"]


def test_a_stopped_read_closes_the_transport():
    closed = []
    stop = threading.Event()
    t = sent_by(SimTransport("healthy"))
    inner = t.send

    def send(cmd, timeout=5.0):
        r = inner(cmd, timeout)
        if cmd == "0100":
            stop.set()                                     # after the PCM answered: there is data to keep
        return r
    t.send, t.close = send, lambda: closed.append(1)
    assert ObdReader(t, stop=stop, retry_pause=0).read().stopped and closed


def test_a_stopped_read_says_what_was_not_read():
    r, _ = stop_read("many_codes", "0100")
    lines = r.lines()
    assert lines[0].endswith(": not read")
    assert any(l.startswith(f"Live values read: 0 of {len(LIVE_PIDS)}") and "not read, stopped by you" in l
               for l in lines)
    assert "Freeze frame not read" in lines and "Stopped by you before the read finished." in lines


def test_a_partly_read_set_of_codes_names_the_modes_not_read():
    r, _ = stop_read("many_codes", "03")
    assert "(modes not read: 07, 0A)" in r.lines()[0] and "P0335 (stored" in r.lines()[0]


def test_event_data_carries_stopped_and_not_read():
    r, _ = stop_read("many_codes", "03")
    d = r.to_event_data()
    assert d["stopped"] is True and d["not_read"] == ["07", "0A", "02"]
    full = read("healthy").to_event_data()
    assert full["stopped"] is False and full["not_read"] == []


def test_a_silent_vehicle_raises_the_specific_error_with_the_same_message():
    t = SimTransport("healthy")
    inner = t.send
    t.send = lambda cmd, timeout=5.0: "NO DATA" if cmd == "0100" else inner(cmd, timeout)
    with pytest.raises(ObdVehicleSilent) as e:
        ObdReader(t, retry_pause=0).read()
    assert isinstance(e.value, ObdProtocolError) and "Is the key on?" in str(e.value)


def test_the_reader_takes_its_interrupt_off_the_transport_when_it_is_done():
    ev = threading.Event()
    done = SimTransport("healthy")
    ObdReader(done, cancel=ev, retry_pause=0).read()
    assert done.cancel is None
    cut = sent_by(SimTransport("healthy"))
    inner = cut.send

    def send(cmd, timeout=5.0):
        if cmd == "010C":
            ev.set()
        return inner(cmd, timeout)
    cut.send = send
    with pytest.raises(ObdCancelled):
        ObdReader(cut, cancel=ev, retry_pause=0).read()
    assert cut.cancel is None                              # else the next heartbeat would see a stale cancel


def test_prepare_runs_the_setup_and_identifies_the_adapter():
    t = sent_by(SimTransport("healthy")); t.open()
    info = ObdReader(t, retry_pause=0).prepare()
    assert t.sent[:len(INIT)] == list(INIT) and "STN2255" in info.stn_version and info.supports_st


def test_held_transport_never_opens_or_closes_the_inner_one():
    inner = SimTransport("healthy"); calls = []
    inner.open, inner.close = lambda: calls.append("open"), lambda: calls.append("close")
    h = HeldTransport(inner)
    h.open(); h.close()
    assert calls == [] and h.sim is True


def test_a_warm_read_skips_the_setup_and_the_identification_and_keeps_the_port_open():
    inner = sent_by(SimTransport("healthy")); inner.open()
    closed = []
    inner.close = lambda: closed.append(1)
    info = ObdReader(inner, retry_pause=0).prepare()          # what the connection did when it connected
    inner.sent.clear()
    r = ObdReader(HeldTransport(inner), adapter=info, retry_pause=0).read()
    assert inner.sent[0] == "0100" and "ATZ" not in inner.sent and "ATI" not in inner.sent
    assert r.adapter is info and r.live and not closed


def test_a_warm_read_does_not_repeat_the_adapter_notes():
    inner = SimTransport("flaky_adapter_id"); inner.open()
    info = ObdReader(inner, retry_pause=0).prepare()
    assert info.notes                                         # STSN timed out at connect
    r = ObdReader(HeldTransport(inner), adapter=info, retry_pause=0).read()
    assert not any("identification incomplete" in n for n in r.notes)


def test_a_cold_read_on_a_held_transport_still_does_the_full_setup():
    inner = sent_by(SimTransport("healthy")); inner.open()
    r = ObdReader(HeldTransport(inner), retry_pause=0).read()
    assert inner.sent[:len(INIT)] == list(INIT) and r.adapter.supports_st
