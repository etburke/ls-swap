import pytest

from crank.obd.adapter import AdapterInfo, clean_id, parse_sti


@pytest.mark.parametrize("text,want", [
    ("STN2255 v4.3.1", "STN2255 v4.3.1"),
    ("\rSTN2255 v4.3.1\r\r", "STN2255 v4.3.1"),
    ("STI\rSTN1110 v2.2.0\r", "STN1110 v2.2.0"),
    ("STN2255 v4.3", "STN2255 v4.3"),
])
def test_parse_sti_accepts_an_stn_version(text, want):
    assert parse_sti(text) == want


@pytest.mark.parametrize("text", ["?", "NO DATA", "STI", "ELM327 v1.5", "@#$%^ \r\x00\x01", "", "STN v1", "OK"])
def test_parse_sti_rejects_everything_else(text):
    assert parse_sti(text) == ""


def test_clean_id_keeps_an_elm_version_line():
    assert clean_id("ELM327 v1.5\r\r") == "ELM327 v1.5"       # the line cleaner in protocol.py would drop this


def test_clean_id_drops_an_echo_blank_lines_and_the_unknown_marker():
    assert clean_id("ATI\rELM327 v1.5\r", "ATI") == "ELM327 v1.5"
    assert clean_id("?\r") == ""
    assert clean_id("") == ""
    assert clean_id("  \r\r") == ""


def test_label_for_an_stn_adapter():
    a = AdapterInfo(elm_version="ELM327 v1.5", stn_version="STN2255 v4.3.1", device="OBDLink MX+", serial="123456")
    assert a.supports_st and a.label() == "OBDLink MX+ (STN2255 v4.3.1, serial 123456)"


def test_label_leaves_out_what_is_unknown():
    assert AdapterInfo(stn_version="STN2255 v4.3.1").label() == "STN adapter (STN2255 v4.3.1)"
    assert AdapterInfo(stn_version="STN2255 v4.3.1", device="OBDLink MX+").label() == "OBDLink MX+ (STN2255 v4.3.1)"


def test_label_for_a_plain_elm327():
    a = AdapterInfo(elm_version="ELM327 v1.5")
    assert not a.supports_st and a.label() == "ELM327 v1.5 (not an STN adapter, no ST features)"


def test_label_when_nothing_was_identified():
    assert AdapterInfo().label() == "unidentified" and not AdapterInfo().supports_st


def test_dict_round_trip():
    a = AdapterInfo("ELM327 v1.5", "STN2255 v4.3.1", "OBDLink MX+", "123456", ["STSN timed out"])
    assert AdapterInfo.from_dict(a.to_dict()) == a


@pytest.mark.parametrize("d", [None, {}, "junk", {"stn_version": None, "notes": "x"}, {"unknown": 1}])
def test_from_dict_tolerates_missing_and_odd_input(d):
    a = AdapterInfo.from_dict(d)
    assert a == AdapterInfo() and a.notes == []
