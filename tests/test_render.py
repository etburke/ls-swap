import pytest

from crank.analysis import analyze_capture
from crank.case import Case, Vehicle
from crank.render import capture_svg, to_html, to_markdown
from crank.sim import generate
from crank.store import SessionStore
from crank.summary import build_summary


@pytest.fixture
def full_case(tmp_path):
    case = Case(SessionStore(tmp_path).open("c"))
    case.set_vehicle(Vehicle(year="1987", make="Toyota", model="FJ60", engine="LY5 5.3L",
                             problem="0 RPM while cranking"))
    case.append("symptom", "No RPM on the scan tool while cranking")
    for scenario, label in (("ref_sag", "baseline"), ("healthy", "after reground")):
        d = generate(scenario, seconds=1.0)
        cid = case.session.add_capture(d, label)
        case.append_capture(cid, label, analyze_capture(d), True)
        if label == "baseline":
            case.append("change", "Re-grounded block to chassis")
    h = case.set_hypothesis(None, "Bad PCM")
    case.set_hypothesis(h.data["hid"], "Bad PCM", "ruled_out", "second PCM behaved the same")
    case.append("measurement", "", {"name": "Battery", "value": 12.6, "unit": "V"})
    return case


def test_markdown_contains_the_recorded_facts(full_case):
    md = to_markdown(build_summary(full_case))
    for needle in ("1987 Toyota FJ60", "LY5 5.3L", "0 RPM while cranking", "Bad PCM: second PCM behaved the same",
                   "Battery: 12.6 V", "5V reference sags", "(SIMULATED)", "Before and after",
                   "Re-grounded block to chassis", "not a diagnosis"):
        assert needle in md, needle


def test_markdown_says_not_recorded_instead_of_inventing(tmp_path):
    case = Case(SessionStore(tmp_path).open("c"))
    case.set_vehicle(Vehicle(make="Toyota"))
    case.append("symptom", "stalls")
    md = to_markdown(build_summary(case))
    assert "Transmission: not recorded" in md and "## Problem\nnot recorded" in md
    assert "None" not in md and "nan" not in md.lower()


def test_empty_case_report_says_so(tmp_path):
    case = Case(SessionStore(tmp_path).open("c"))
    assert "Nothing has been recorded" in to_markdown(build_summary(case))
    assert "Nothing has been recorded" in to_html(build_summary(case))


def test_html_is_self_contained_and_escapes_user_text(full_case):
    full_case.append("observation", "<script>alert(1)</script> & <img src=x onerror=1>")
    s = build_summary(full_case)
    html = to_html(s, {c["id"]: capture_svg(full_case.session.load_capture(c["id"])) for c in s.captures})
    assert "<script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img" not in html
    assert "http://" not in html and "https://" not in html
    assert "<svg" in html and "SIMULATED" in html


def test_capture_svg_handles_missing_channels():
    d = generate("healthy", seconds=1.0)
    d.channels = {2: d.channels[2]}
    assert capture_svg(d) == ""


# --- final-review fixes
def test_missing_supply_numbers_read_not_recorded_never_zero(tmp_path):
    case = Case(SessionStore(tmp_path).open("c"))
    case.append("capture", "baseline", {"sim": False, "snapshot": {
        "verdict": [{"code": "x", "severity": "info", "title": "Crank signal looks clean"}],
        "edges": 10, "revs_good": 1, "revs": 1, "rpm": 200, "ref_sag_mv": None, "gnd_dev_mv": None}},
        {"capture": "001"})
    s = build_summary(case)
    md, html = to_markdown(s), to_html(s)
    assert "5V ref sag not recorded" in md and "ground deviation not recorded" in md
    assert "sag 0 mV" not in md and "deviation 0 mV" not in md and "not recorded" in html


def test_labels_containing_a_colon_stay_intact_in_html(tmp_path):
    case = Case(SessionStore(tmp_path).open("c"))
    case.append("capture", "after swap: new sensor", {"sim": True, "snapshot": None}, {"capture": "001"})
    html = to_html(build_summary(case))
    assert "<b>001 after swap: new sensor</b>" in html and "SIMULATED" in html
    assert "new sensor (SIMULATED)" not in html           # the label must not leak into the tail


def test_code_status_and_inferred_pairs_are_shown(tmp_path):
    case = Case(SessionStore(tmp_path).open("c"))
    case.append("code_read", "", {"codes": [{"code": "P0335", "status": "pending"}]})
    for lab in ("A", "B"):
        if lab == "B":
            case.append("change", "swapped sensor")
        case.append("capture", lab, {"sim": False, "snapshot": None}, {"capture": lab})
    md = to_markdown(build_summary(case))
    assert "P0335 (pending)" in md and "(inferred from the order of events)" in md


def test_markdown_flattens_multiline_notes_and_escapes_dollars(tmp_path):
    case = Case(SessionStore(tmp_path).open("md"))
    case.append("observation", "line one\n# not a heading\nvolts $5 and $6")
    md = to_markdown(build_summary(case))
    line = next(ln for ln in md.splitlines() if "line one" in ln)
    assert "# not a heading" in line and "\\$5 and \\$6" in line
    assert "$5" not in md.replace("\\$5", "")


def test_vehicle_only_case_has_a_timeline_placeholder(tmp_path):
    case = Case(SessionStore(tmp_path).open("v"))
    case.set_vehicle(Vehicle(make="Toyota"))
    s = build_summary(case)
    assert "- nothing recorded yet" in to_markdown(s)
    assert "nothing recorded yet" in to_html(s)
