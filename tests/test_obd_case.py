import pytest

from crank.case import Case
from crank.obd.reader import ObdReader
from crank.obd.transport import SimTransport
from crank.render import to_html, to_markdown
from crank.store import SessionStore
from crank.summary import build_summary


@pytest.fixture
def case(tmp_path):
    return Case(SessionStore(tmp_path).open("c"))


def read(name):
    return ObdReader(SimTransport(name)).read()


def test_a_read_appends_one_code_read_and_a_measurement_per_supported_value(case):
    r = read("p2119_zero_rpm")
    events = case.append_obd(r)
    assert [e.type for e in events].count("code_read") == 1
    cr = events[0]
    assert cr.data["source"] == "obd" and cr.data["sim"] is True and cr.data["codes"][0]["code"] == "P2119"
    meas = [e for e in events if e.type == "measurement"]
    assert len(meas) == sum(v.supported for v in r.live) and all(e.refs["obd"] == cr.id for e in meas)


def test_unsupported_values_append_no_measurement(case):
    events = case.append_obd(read("limited_pcm"))
    names = {e.data["name"] for e in events if e.type == "measurement"}
    assert names == {"Engine RPM", "Coolant temperature"}


def test_a_clean_read_is_recorded_as_an_empty_code_list(case):
    events = case.append_obd(read("healthy"))
    assert events[0].type == "code_read" and events[0].data["codes"] == []


def test_manual_code_read_still_needs_a_code(case):
    with pytest.raises(ValueError):
        case.append("code_read", "", {"codes": [], "source": "manual"})


def test_zero_rpm_condition_does_not_claim_the_engine_is_off(case):
    events = case.append_obd(read("p2119_zero_rpm"))
    rpm = next(e for e in events if e.data.get("name") == "Engine RPM")
    assert rpm.data["value"] == 0.0 and "engine off" not in rpm.data["conditions"]
    assert rpm.data["conditions"] == "OBD, PCM reports 0 RPM"


def test_condition_when_rpm_is_not_answered(case):
    r = read("limited_pcm")
    for v in r.live:
        if v.name == "Engine RPM":
            v.value = None
    events = case.append_obd(r)
    others = [e for e in events if e.type == "measurement"]
    assert others and all(e.data["conditions"] == "OBD, PCM reported no RPM" for e in others)


def test_running_condition_when_rpm_is_nonzero(case):
    events = case.append_obd(read("healthy"))
    cond = next(e for e in events if e.data.get("name") == "Engine RPM").data["conditions"]
    assert cond == "OBD, PCM reports 750 RPM" and "running" not in cond


def test_summary_carries_status_freeze_frame_and_simulated_flag(case):
    case.append_obd(read("p2119_zero_rpm"))
    s = build_summary(case)
    assert [(c["code"], c["status"], c["sim"]) for c in s.codes] == [("P2119", "stored", True)]
    assert s.freeze_frame["code"] == "P2119" and s.freeze_frame["sim"] is True
    assert all(m["sim"] for m in s.measurements)


def test_a_later_clean_read_marks_earlier_codes_as_not_in_latest_read(case):
    case.append_obd(read("p2119_zero_rpm"))
    case.append_obd(read("healthy"))
    s = build_summary(case)
    assert s.codes[0]["code"] == "P2119" and s.codes[0]["status"] == "not in latest OBD read"


def test_a_code_missing_from_a_later_obd_read_does_not_erase_manual_history(case):
    case.append("code_read", "", {"codes": [{"code": "P0335", "status": "history"}], "source": "manual"})
    case.append_obd(read("p2119_zero_rpm"))
    got = {c["code"]: c["status"] for c in build_summary(case).codes}
    assert got == {"P0335": "not in latest OBD read", "P2119": "stored"}


def test_timeline_lines_for_obd_reads(case):
    case.append_obd(read("healthy"))
    case.append_obd(read("p2119_zero_rpm"))
    lines = [t["line"] for t in build_summary(case).timeline if t["type"] == "code_read"]
    assert lines == ["OBD read [SIMULATED]: no stored, pending or permanent codes",
                     "OBD read [SIMULATED]: P2119 (stored)"]


def test_reports_label_simulated_obd_data_and_show_the_freeze_frame(case):
    case.append_obd(read("p2119_zero_rpm"))
    md = to_markdown(build_summary(case))
    assert "P2119 (stored) [SIMULATED]" in md and "Freeze frame [SIMULATED] for P2119" in md
    assert "Engine RPM: 0 rpm [SIMULATED]" in md
    html = to_html(build_summary(case))
    assert "P2119 (stored) [SIMULATED]" in html and "Freeze frame [SIMULATED]" in html


def _unread(modes):
    r = read("healthy")
    r.unsupported_modes = list(modes)
    return r


def test_earlier_stored_code_is_not_demoted_when_mode_03_was_not_read(case):
    case.append_obd(read("p2119_zero_rpm"))
    case.append_obd(_unread(["03"]))
    s = build_summary(case)
    assert s.codes[0]["status"] == "stored"
    line = [t["line"] for t in s.timeline if t["type"] == "code_read"][-1]
    assert "modes not read: 03" in line and "no stored, pending or permanent codes" not in line
    assert "no codes reported by the modes that were read" in line


def test_other_modes_still_demote_codes(case):
    case.append_obd(read("p2119_zero_rpm"))
    case.append_obd(_unread(["07"]))
    assert build_summary(case).codes[0]["status"] == "not in latest OBD read"


def test_a_clean_later_read_clears_the_stale_freeze_frame(case):
    case.append_obd(read("many_codes"))
    assert build_summary(case).freeze_frame["code"] == "P0335"
    case.append_obd(read("healthy"))
    assert build_summary(case).freeze_frame is None


def test_mode_02_unsupported_leaves_the_freeze_frame_unchanged(case):
    case.append_obd(read("many_codes"))
    case.append_obd(_unread(["02"]))
    assert build_summary(case).freeze_frame["code"] == "P0335"


def test_one_engine_rpm_row_the_latest_read_wins(case):
    case.append_obd(read("healthy"))
    case.append_obd(read("p2119_zero_rpm"))
    s = build_summary(case)
    rows = [m for m in s.measurements if m["name"] == "Engine RPM"]
    assert len(rows) == 1 and rows[0]["value"] == 0
    md = to_markdown(s)
    assert len([ln for ln in md.splitlines() if "Engine RPM" in ln and ln.startswith("- Engine RPM")]) == 1


def test_reports_escape_obd_text(case):
    r = read("p2119_zero_rpm")
    r.codes[0]["description"] = "<script>alert(1)</script>"
    case.append_obd(r)
    assert "<script>" not in to_html(build_summary(case))


def test_timeline_labels_simulated_measurements_only(case):
    case.append_obd(read("p2119_zero_rpm"))
    case.append("measurement", "", {"name": "Battery", "value": 12.4, "unit": "V"})
    lines = [t["line"] for t in build_summary(case).timeline if t["type"] == "measurement"]
    assert len(lines) > 1
    for line in lines:
        assert ("[SIMULATED]" in line) == (not line.startswith("Measured Battery"))


def test_unknown_dtc_description_reads_as_not_recorded(case):
    r = read("p2119_zero_rpm")
    r.codes[0]["description"] = ""
    r.freeze_frame["description"] = ""
    case.append_obd(r)
    s = build_summary(case)
    md = to_markdown(s)
    assert "P2119 (stored) [SIMULATED] description not recorded" in md
    assert "Freeze frame [SIMULATED] for P2119 description not recorded:" in md


def test_freeze_frame_text_is_escaped(case):
    r = read("p2119_zero_rpm")
    r.freeze_frame["description"] = "<b>x</b>"
    case.append_obd(r)
    assert "<b>x</b>" not in to_html(build_summary(case))


def test_manual_codes_after_an_obd_read_are_kept(case):
    case.append_obd(read("p2119_zero_rpm"))
    case.append("code_read", "", {"codes": [{"code": "P0335", "description": "", "status": "stored"}]})
    codes = {c["code"] for c in build_summary(case).codes}
    assert {"P2119", "P0335"} <= codes
