import pytest

from crank.obd import protocol as P
from crank.obd.errors import ObdAdapterError, ObdProtocolError


def test_request_strings():
    assert P.request(1, 0x0C) == "010C"
    assert P.request(3) == "03"
    assert P.request(2, 0x0C, frame=True) == "020C00"


def test_single_frame_is_stripped_of_noise_and_decoded():
    resp = "SEARCHING...\r41 0C 1A F8 \r\r>"
    assert P.response_data(resp, 1, 0x0C) == bytes([0x1A, 0xF8])
    assert P.decode_pid(0x0C, bytes([0x1A, 0xF8])) == (1726.0, "rpm")


def test_multi_frame_can_response_is_reassembled_and_truncated():
    resp = "009\r0: 43 04 01 33 02 36\r1: 00 16 00 00 00 00 00\r>"
    assert P.response_data(resp, 3) == bytes([0x04, 0x01, 0x33, 0x02, 0x36, 0x00, 0x16, 0x00])


def test_no_data_is_none_not_empty():
    assert P.response_data("NO DATA\r>", 1, 0x0C) is None


@pytest.mark.parametrize("text", ["UNABLE TO CONNECT", "BUS INIT: ERROR", "CAN ERROR", "?", "STOPPED"])
def test_adapter_errors_raise(text):
    with pytest.raises(ObdProtocolError):
        P.messages(text)


def test_garbage_raises_with_a_readable_message():
    with pytest.raises(ObdProtocolError, match="Unreadable"):
        P.messages("@#$%^")


def test_odd_length_hex_is_unreadable_not_silently_trimmed():
    with pytest.raises(ObdProtocolError):
        P.messages("410C1AF")


def test_negative_response_means_mode_not_supported():
    from crank.obd.errors import ObdNotSupported
    with pytest.raises(ObdNotSupported):
        P.response_data("7F 0A 11", 0x0A)


def test_reply_to_a_different_request_is_an_error():
    with pytest.raises(ObdProtocolError, match="Unexpected reply"):
        P.response_data("41 05 7B", 1, 0x0C)


@pytest.mark.parametrize("b0,b1,code", [(0x03, 0x35, "P0335"), (0x41, 0x00, "C0100"), (0x81, 0x23, "B0123"),
                                         (0xC1, 0x00, "U0100"), (0x21, 0x19, "P2119")])
def test_dtc_letters_digits_and_hex(b0, b1, code):
    assert P.decode_dtc(b0, b1) == code


def test_can_dtc_list_skips_count_byte_and_padding():
    assert P.decode_dtcs(bytes([2, 0x03, 0x35, 0x21, 0x19, 0, 0]), can=True) == ["P0335", "P2119"]


def test_non_can_dtc_list_has_no_count_byte():
    assert P.decode_dtcs(bytes([0x03, 0x35, 0, 0, 0, 0]), can=False) == ["P0335"]


def test_empty_dtc_reply_is_no_codes():
    assert P.decode_dtcs(b"", can=True) == []


def test_supported_pid_bitmap():
    sup = P.supported_pids(0x00, bytes([0xBE, 0x1F, 0xA8, 0x13]))
    assert {0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0C, 0x0D, 0x0F, 0x11, 0x13, 0x15, 0x1C, 0x1F, 0x20} <= sup
    assert 0x02 not in sup and 0x08 not in sup


@pytest.mark.parametrize("pid,data,value", [
    (0x04, bytes([0xFF]), 100.0), (0x05, bytes([0x82]), 90), (0x06, bytes([0x80]), 0.0),
    (0x0C, bytes([0, 0]), 0.0), (0x0D, bytes([60]), 60), (0x11, bytes([0]), 0.0),
    (0x42, bytes([0x36, 0xB0]), 14.0), (0x0F, bytes([0x4B]), 35)])
def test_pid_formulas(pid, data, value):
    assert P.decode_pid(pid, data)[0] == pytest.approx(value)


def test_short_pid_data_is_undecodable_not_zero():
    assert P.decode_pid(0x0C, bytes([0x1A])) is None


@pytest.mark.parametrize("text,can", [("A6", True), ("6", True), ("A9", True), ("A2", False), ("1", False)])
def test_protocol_number_means_can_or_not(text, can):
    assert P.is_can(text) is can


def test_known_and_unknown_descriptions():
    assert "Throttle" in P.describe_dtc("P2119") and P.describe_dtc("P9999") is None


def test_positive_message_wins_over_response_pending():
    assert P.response_data("7F 03 78\r43 01 21 19", 0x03) == bytes([0x01, 0x21, 0x19])


def test_only_response_pending_is_a_protocol_error_not_unsupported():
    from crank.obd.errors import ObdNotSupported
    with pytest.raises(ObdProtocolError) as ei:
        P.response_data("7F 03 78", 0x03)
    assert not isinstance(ei.value, ObdNotSupported)


def test_busy_negative_response_is_a_protocol_error_not_unsupported():
    from crank.obd.errors import ObdNotSupported
    with pytest.raises(ObdProtocolError) as ei:
        P.response_data("7F 03 21", 0x03)
    assert not isinstance(ei.value, ObdNotSupported)
    assert "busy" in str(ei.value) and "21" in str(ei.value)


@pytest.mark.parametrize("nrc", ["11", "12", "31"])
def test_unsupported_negative_responses_raise_not_supported(nrc):
    from crank.obd.errors import ObdNotSupported
    with pytest.raises(ObdNotSupported):
        P.response_data(f"7F 03 {nrc}", 0x03)


def test_ecu_single_frame_with_and_without_spaces():
    want = {"7E8": bytes.fromhex("4100BE3FA813")}
    assert P.ecu_messages("7E8 06 41 00 BE 3F A8 13\r>") == want
    assert P.ecu_messages("7E8064100BE3FA813\r\r>") == want


def test_ecu_single_frame_padding_is_stripped():
    assert P.ecu_messages("7E8 02 41 00 AA AA AA AA AA") == {"7E8": bytes.fromhex("4100")}


def test_ecu_short_single_frame_is_an_error():
    with pytest.raises(ObdProtocolError):
        P.ecu_messages("7E8 06 41 00")


def test_ecu_multi_frame_is_reassembled_and_truncated():
    resp = "7E8 10 0A 43 03 01 33 02 36\r7E8 21 00 16 00 00 00 00 00\r>"
    assert P.ecu_messages(resp) == {"7E8": bytes.fromhex("43030133023600160000")}


def test_two_ecus_interleaved_in_either_order():
    a = ["7E8 10 0A 43 03 01 33 02 36", "7E8 21 00 16 00 00 00 00 00"]
    b = ["7E9 10 08 43 02 07 00 07 01", "7E9 21 07 02 00 00 00 00 00"]
    want = {"7E8": bytes.fromhex("43030133023600160000"), "7E9": bytes.fromhex("4302070007010702")}
    assert P.ecu_messages("\r".join([a[0], b[0], a[1], b[1]])) == want
    assert P.ecu_messages("\r".join([b[0], a[0], b[1], a[1]])) == want


def test_missing_or_out_of_order_consecutive_frame_is_an_error():
    with pytest.raises(ObdProtocolError, match="7E8"):
        P.ecu_messages("7E8 10 0A 43 03 01 33 02 36")
    with pytest.raises(ObdProtocolError, match="7E8"):
        P.ecu_messages("7E8 10 0A 43 03 01 33 02 36\r7E8 22 00 16 00 00 00 00 00")
    with pytest.raises(ObdProtocolError, match="7E8"):
        P.ecu_messages("7E8 21 00 16 00 00 00 00 00")


def test_ecu_no_data_is_none_and_adapter_errors_raise():
    assert P.ecu_messages("NO DATA\r>") is None
    with pytest.raises(ObdProtocolError):
        P.ecu_messages("CAN ERROR")
    with pytest.raises(ObdProtocolError):
        P.ecu_messages("?")


@pytest.mark.parametrize("text", ["18DAF110064100BE3FA8", "48 6B 10 41 0C 1A F8 3C", "@#$%^"])
def test_unsupported_header_shapes_are_rejected_readably(text):
    with pytest.raises(ObdProtocolError, match="11-bit CAN|Unreadable"):
        P.ecu_messages(text)


def test_by_ecu_returns_positive_data_and_lists_not_supported_ecus():
    resp = "7E8 03 7F 03 11 00 00 00 00\r7E9 04 43 01 01 33 00 00 00"
    assert P.response_data_by_ecu(resp, 3) == ({"7E9": bytes([0x01, 0x01, 0x33])}, ["7E8"])


def test_by_ecu_busy_negative_response_names_the_ecu():
    with pytest.raises(ObdProtocolError, match="7E8"):
        P.response_data_by_ecu("7E8 03 7F 03 22 00 00 00 00", 3)


def test_by_ecu_no_data_and_wrong_reply():
    assert P.response_data_by_ecu("NO DATA", 3) is None
    with pytest.raises(ObdProtocolError):
        P.response_data_by_ecu("7E8 04 41 0C 1A F8 00 00 00", 1, 0x0D)


def test_primary_ecu_is_the_lowest_header():
    assert P.primary_ecu(["7E9", "7E8"]) == "7E8"
    assert P.primary_ecu({"7EA": b"", "7E9": b""}) == "7E9"


@pytest.mark.parametrize("text,kind,transient,reset", [
    ("BUS BUSY", "BUS BUSY", True, False),
    ("BUS ERROR", "BUS ERROR", True, False),
    ("CAN ERROR", "CAN ERROR", True, False),
    ("FB ERROR", "FB ERROR", True, False),
    ("<RX ERROR", "<RX ERROR", True, False),
    ("DATA ERROR", "DATA ERROR", True, False),
    ("FC RX TIMEOUT", "FC RX TIMEOUT", True, False),
    ("LV RESET", "LV RESET", False, True),
    ("BUFFER FULL", "BUFFER FULL", False, False),
    ("OUT OF MEMORY", "OUT OF MEMORY", False, False),
    ("UART RX OVERFLOW", "UART RX OVERFLOW", False, False),
    ("STOPPED", "STOPPED", False, False),
    ("ACT ALERT", "ACT ALERT", False, False),
    ("ERR94", "ERR", False, False),
    ("UNABLE TO CONNECT", "UNABLE TO CONNECT", False, False),
    ("BUS INIT: ERROR", "BUS INIT", False, False),
])
def test_adapter_errors_are_classified(text, kind, transient, reset):
    with pytest.raises(ObdAdapterError) as e:
        P.ecu_messages(text)
    assert (e.value.kind, e.value.transient, e.value.reset) == (kind, transient, reset)


def test_no_vehicle_errors_ask_about_the_key():
    for text in ("UNABLE TO CONNECT", "BUS INIT: ERROR"):
        with pytest.raises(ObdAdapterError, match="Is the key on"):
            P.ecu_messages(text)


def test_lv_reset_points_at_adapter_power():
    with pytest.raises(ObdAdapterError, match="pin 16"):
        P.ecu_messages("LV RESET")


def test_data_lines_do_not_hide_an_adapter_error():
    with pytest.raises(ObdAdapterError) as e:
        P.ecu_messages("7E8 06 41 00 BE 3F A8 13\rCAN ERROR")
    assert e.value.transient


def test_unknown_command_is_still_a_plain_protocol_error():
    with pytest.raises(ObdProtocolError) as e:
        P.ecu_messages("?")
    assert not isinstance(e.value, ObdAdapterError)


@pytest.mark.parametrize("text,kind", [("CAN ERROR", "CAN ERROR"), ("\rLV RESET\r", "LV RESET"),
                                       ("BUFFER FULL", "BUFFER FULL")])
def test_raise_if_adapter_error_raises_the_specific_error(text, kind):
    with pytest.raises(ObdAdapterError) as e:
        P.raise_if_adapter_error(text)
    assert e.value.kind == kind


@pytest.mark.parametrize("text", ["?", "STN2255 v4.3.1", "ELM327 v1.5", "OBDLink MX+ 1234", "OK", "", "NO DATA"])
def test_raise_if_adapter_error_ignores_ordinary_replies(text):
    P.raise_if_adapter_error(text)


def test_ecu_reply_positive_and_negative_per_ecu():
    resp = "7E8 03 7F 01 22\r7E9 06 41 00 BE 3F A8 13"
    assert P.ecu_reply(resp, 1, 0, ecu="7E8") == (None, 0x22)
    assert P.ecu_reply(resp, 1, 0, ecu="7E9") == (bytes.fromhex("BE3FA813"), None)
    assert P.ecu_reply(resp, 1, 0, ecu="7EA") is None


def test_ecu_reply_no_data_is_none():
    assert P.ecu_reply("NO DATA", 1, 0x0C, ecu="7E8") is None


def test_ecu_reply_ignores_odd_replies_from_other_ecus():
    resp = "7E8 04 41 0C 0B B8\r7E9 03 41 0D 05"        # 7E9 answers the wrong PID
    assert P.ecu_reply(resp, 1, 0x0C, ecu="7E8") == (bytes([0x0B, 0xB8]), None)


def test_ecu_reply_raises_on_an_unexpected_reply_from_the_wanted_ecu():
    with pytest.raises(ObdProtocolError):
        P.ecu_reply("7E8 03 41 0D 05", 1, 0x0C, ecu="7E8")


def test_busy_nrcs():
    assert P.BUSY_NRC == (0x21, 0x22, 0x78)


@pytest.mark.parametrize("pid,data,value", [(0x0E, b"\x8c", 6.0), (0x4C, b"\x54", 32.94), (0x44, b"\x80\x00", 1.0),
                                            (0x10, b"\x01\xf4", 5.0), (0x46, b"\x30", 8)])
def test_new_pid_decodes(pid, data, value):
    assert P.decode_pid(pid, data)[0] == pytest.approx(value, abs=0.01)


def test_poll_pids_exclude_rpm_and_cover_table():
    from crank.obd.pids import PIDS, POLL_PIDS
    assert 0x0C not in POLL_PIDS and set(POLL_PIDS) | {0x0C} == set(PIDS)


def test_request_multi_builds_and_limits():
    assert P.request_multi([0x0C, 0x0E, 0x11]) == "010C0E11"
    with pytest.raises(ValueError):
        P.request_multi([0x0C] * 7)
    with pytest.raises(ValueError):
        P.request_multi([])


def test_split_multi_any_order_and_missing():
    data = bytes([0x11, 0x54, 0x0C, 0x02, 0x08, 0x0E, 0x8C])      # 11, then 0C, then 0E; 4C left out
    assert P.split_multi(data, [0x0C, 0x0E, 0x11, 0x4C]) == {0x11: b"\x54", 0x0C: b"\x02\x08", 0x0E: b"\x8c"}


def test_split_multi_truncated_value_dropped():
    assert P.split_multi(bytes([0x0C, 0x02]), [0x0C]) == {}


def test_split_multi_stops_at_an_unrequested_pid():
    assert P.split_multi(bytes([0x0C, 0x02, 0x08, 0x99, 0x01, 0x11, 0x54]), [0x0C, 0x11]) == {0x0C: b"\x02\x08"}


def test_multi_reply_across_isotp_frames():
    from crank.obd.transport import _isotp
    body = bytes([0x41, 0x0C, 0x02, 0x08, 0x0E, 0x8C, 0x11, 0x54, 0x45, 0x5C, 0x47, 0x00, 0x49, 0x10])
    resp = "\r".join("7E8" + f.hex().upper() for f in _isotp(body))
    assert resp.count("\r") >= 1                                    # really several frames
    msg = P.ecu_messages(resp)["7E8"]
    got = P.split_multi(msg[1:], [0x0C, 0x0E, 0x11, 0x45, 0x47, 0x49])
    assert set(got) == {0x0C, 0x0E, 0x11, 0x45, 0x47, 0x49} and got[0x49] == b"\x10"
