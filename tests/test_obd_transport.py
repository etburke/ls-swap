from crank.obd import protocol as P
from crank.obd.transport import SimTransport


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
