import pytest

from crank.obd.canframes import Frame, decode, parse_line


def test_parses_spaced_and_unspaced_lines_the_same():
    a = parse_line("0C9 8 00 0C 80 00 00 00 00 00")
    b = parse_line("0C98000C800000000000")
    assert a == b == Frame("0C9", 8, bytes([0, 0x0C, 0x80, 0, 0, 0, 0, 0]))


def test_0c9_engine_speed_is_bytes_1_2_big_endian_quarter_rpm():
    (dec, rpm), = decode(parse_line("0C9 8 00 0C 80 00 00 00 00 00"))
    assert dec.name == "Engine speed" and rpm == 0x0C80 / 4 == 800.0
    assert dec.version.startswith("0C9-engine-speed-v1")


@pytest.mark.parametrize("junk", ["", "SEARCHING...", "CAN ERROR", "BUFFER FULL", "0C9 8 00 0C", "STOPPED", "?", "7E8"])
def test_junk_and_partial_lines_do_not_parse(junk):
    assert parse_line(junk) is None


def test_unknown_ids_parse_but_decode_to_nothing():
    assert decode(parse_line("1A1 2 01 02")) == []


def test_short_0c9_decodes_to_nothing():
    assert decode(parse_line("0C9 2 00 0C")) == []
