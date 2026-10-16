import pytest

from crank.obd import protocol as P
from crank.obd.errors import ObdTimeout
from crank.obd.transport import ADAPTERS, SCENARIOS, SimTransport


def sim(name, headers=True):
    t = SimTransport(name)
    t.open()
    t.send("ATZ")
    t.send("ATH1" if headers else "ATH0")
    return t


def test_headers_off_output_is_unchanged():
    t = sim("p2119_zero_rpm", headers=False)
    assert t.send("010C") == "410C0000"
    assert t.send("03") == "43012119"


def test_headers_on_single_ecu_reply_parses_to_the_pcm():
    t = sim("p2119_zero_rpm")
    assert P.ecu_messages(t.send("010C")) == {"7E8": bytes([0x41, 0x0C, 0, 0])}


def test_atz_resets_headers_to_off():
    t = sim("p2119_zero_rpm")
    t.send("ATZ")
    assert not t.send("010C").startswith("7E8")


def test_pcm_and_trans_both_answer_mode_03_only():
    t = sim("pcm_and_trans")
    msgs = P.ecu_messages(t.send("03"))
    assert set(msgs) == {"7E8", "7E9"} and msgs["7E9"][:2] == bytes([0x43, 0x01])
    assert set(P.ecu_messages(t.send("0100"))) == {"7E8"}


def test_interleaved_multiframe_frames_really_interleave():
    t = sim("interleaved_multiframe")
    lines = t.send("03").split("\r")
    assert [l[:3] for l in lines[:2]] == ["7E8", "7E9"]
    msgs = P.ecu_messages("\r".join(lines))
    assert len(msgs["7E8"]) == 8 and len(msgs["7E9"]) == 8


def test_rejecting_ecu_sends_a_negative_response():
    t = sim("trans_rejects_mode")
    data, unsupported = P.response_data_by_ecu(t.send("0A"), 0x0A)
    assert unsupported == ["7E9"]


def test_pcm_header_can_move():
    t = sim("pcm_not_7e8")
    assert set(P.ecu_messages(t.send("0100"))) == {"7E9"}


def test_search_delay_beyond_the_timeout_times_out_the_first_request_only():
    t = sim("slow_search")
    t.send("ATSP0")
    with pytest.raises(ObdTimeout):
        t.send("0100", 5.0)
    assert t.send("0100", 20.0).startswith("7E8")
    assert t.send("010C", 5.0).startswith("7E8")


def test_injected_error_is_answered_once_then_normal():
    t = sim("transient_can_error")
    assert t.send("010C") == "CAN ERROR"
    assert t.send("010C").startswith("7E8")
    assert t.send("0105").startswith("7E8")


def test_persistent_error_keeps_coming():
    t = sim("persistent_can_error")
    assert [t.send("010C") for _ in range(4)] == ["CAN ERROR"] * 4


def test_lv_reset_makes_the_sim_forget_headers_and_protocol():
    t = sim("lv_reset_mid_read")
    assert t.send("010C") == "LV RESET"
    assert t.headers is False and t.searched is False
    assert not t.send("010C").startswith("7E8")        # headerless until ATH1 is sent again


def test_buffer_full_scenario_answers_the_error_text():
    assert sim("buffer_full").send("010C") == "BUFFER FULL"


def _open(name):
    t = SimTransport(name)
    t.open()
    return t


def test_the_stn_preset_answers_every_id_command():
    t = _open("healthy")
    assert t.send("ATI") == "ELM327 v1.5"
    assert t.send("STI") == "STN2255 v4.3.1"
    assert t.send("STDI") == "OBDLink MX+ 1234"
    assert t.send("STSN") == "110012345678"


def test_a_clone_answers_question_marks_to_st_commands():
    t = _open("elm_clone")
    assert t.send("ATI") == "ELM327 v1.5"
    assert t.send("STI") == "?" and t.send("STDI") == "?" and t.send("STSN") == "?"


def test_the_flaky_preset_times_out_on_the_serial_number_only():
    t = _open("flaky_adapter_id")
    assert t.send("STI") == "STN2255 v4.3.1"
    with pytest.raises(ObdTimeout):
        t.send("STSN")


def test_every_scenario_names_a_known_adapter_preset():
    assert {s.adapter for s in SCENARIOS.values()} <= set(ADAPTERS)
    assert "elm_clone" in SCENARIOS and "flaky_adapter_id" in SCENARIOS
