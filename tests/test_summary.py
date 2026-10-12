import pytest

from crank.analysis import analyze_capture
from crank.case import Case, Vehicle
from crank.sim import generate
from crank.store import SessionStore
from crank.summary import NOT_RECORDED, build_summary


@pytest.fixture
def case(tmp_path):
    return Case(SessionStore(tmp_path).open("c"))


def _capture(case, scenario, label):
    d = generate(scenario, seconds=1.0)
    cid = case.session.add_capture(d, label)
    case.append_capture(cid, label, analyze_capture(d), True)
    return cid


def test_empty_case_is_a_valid_empty_summary(case):
    s = build_summary(case)
    assert s.empty and s.timeline == [] and s.vehicle.title == "Vehicle not recorded"
    assert len(s.not_tried) == 5


def test_profile_only_case_is_not_empty(case):
    case.set_vehicle(Vehicle(make="Toyota", problem="0 RPM"))
    assert not build_summary(case).empty


def test_ruled_out_carries_reason_and_evidence(case):
    cid = _capture(case, "healthy", "baseline")
    h = case.set_hypothesis(None, "Bad PCM")
    case.set_hypothesis(h.data["hid"], "Bad PCM", "ruled_out", "second PCM behaved the same", {"capture": cid})
    case.set_hypothesis(None, "Sensor air gap")
    s = build_summary(case)
    assert s.ruled_out == [{"title": "Bad PCM", "reason": "second PCM behaved the same",
                            "evidence": [f"capture {cid}"]}]
    assert [o["title"] for o in s.open] == ["Sensor air gap"]


def test_latest_measurement_and_active_codes(case):
    case.append("measurement", "", {"name": "Battery", "value": 12.1, "unit": "V"})
    case.append("measurement", "", {"name": "Battery", "value": 12.6, "unit": "V"})
    case.append("code_read", "", {"codes": [{"code": "P2119", "status": "current"}]})
    case.append("code_read", "", {"codes": [{"code": "P0335", "status": "pending"}]})
    s = build_summary(case)
    assert [m["value"] for m in s.measurements] == [12.6]
    assert [c["code"] for c in s.codes] == ["P0335"]


def test_not_yet_tried_shrinks_as_steps_get_data(case):
    from crank.procedure import step_by_id
    case.log_guided_step(step_by_id("static"), {"battery_v": 12.6, "gnd_batt_block_ohm": 0.4,
                                                "gnd_block_chassis_ohm": 0.3, "gnd_batt_pcm_ohm": 0.5})
    _capture(case, "healthy", "baseline")
    left = build_summary(case).not_tried
    assert all("Static baseline" not in t and "Baseline cranking" not in t for t in left)
    assert any("Key on, engine off" in t for t in left)


def test_before_after_only_when_a_change_separates_two_captures(case):
    _capture(case, "ref_sag", "baseline")
    assert build_summary(case).before_after == []            # no change yet
    case.append("change", "Re-grounded block to chassis")
    assert build_summary(case).before_after == []            # nothing after it yet
    _capture(case, "healthy", "after reground")
    pair = build_summary(case).before_after[0]
    assert pair["change"] == "Re-grounded block to chassis"
    assert pair["before"]["label"] == "baseline" and pair["after"]["label"] == "after reground"


def test_explicit_before_after_link_overrides_inference(case):
    a = _capture(case, "ref_sag", "A")
    b = _capture(case, "healthy", "B")
    c = _capture(case, "ground_lift", "C")
    case.append("change", "swapped sensor", {}, {"before": a, "after": c})
    pair = build_summary(case).before_after[0]
    assert (pair["before"]["id"], pair["after"]["id"]) == (a, c) and b


def test_simulated_flag_and_missing_verdict_are_carried(case):
    cid = case.session.add_capture(generate("healthy", seconds=1.0), "old")      # legacy, no snapshot
    s = build_summary(case)
    assert s.captures[0]["snapshot"] is None and s.captures[0]["id"] == cid
    assert s.attachments == [f"captures/{cid}.npz"]


def test_skipped_log_lines_are_reported(case):
    case.append("symptom", "x")
    with open(case.path / "events.jsonl", "a") as f:
        f.write("oops\n")
    assert build_summary(case).skipped_lines == 1
