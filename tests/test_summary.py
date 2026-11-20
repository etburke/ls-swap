import pytest

from crank.analysis import analyze_capture
from crank.case import Case, Vehicle
from crank.sim import generate
from crank.store import SessionStore
from crank.summary import NOT_RECORDED, build_summary, describe


def _raw_meta(**kw):
    m = {"id": "001", "label": "", "mode": "raw", "sim": True, "seconds": 18.4, "end_reason": "time",
         "notes": [], "capture_id": None,
         "summary": {"lines": 1512, "frames": 1512, "max_gap_s": 0.02,
                     "ids": {"0C9": {"count": 900, "hz": 50.0}, "1A1": {"count": 400, "hz": 20.0},
                             "3C1": {"count": 212, "hz": 10.0}},
                     "engine_speed": {"decoder": "0C9-engine-speed-v1: bytes[1:3] BE x0.25", "best_effort": True,
                                      "samples": 900, "max": 212.25, "median_nonzero": 180.0,
                                      "first_nonzero_s": 2.0}}}
    m.update(kw)
    return m


def test_describe_raw_watch_says_best_effort_and_sim(tmp_path):
    case = Case(SessionStore(tmp_path).open("c"))
    ev = case.append_watch(_raw_meta())
    assert describe(ev) == ("OBD watch 001 [SIMULATED] (raw CAN, 18.4 s): 1,512 frames from 3 IDs; "
                            "engine speed (best-effort, ID 0C9) max 212 rpm")
    meta = _raw_meta(id="002", sim=False, end_reason="buffer_full")
    meta["summary"] = {**meta["summary"], "engine_speed": None}
    text = describe(case.append_watch(meta))
    assert text.startswith("OBD watch 002 (raw CAN, 18.4 s): 1,512 frames from 3 IDs; no engine speed frames seen")
    assert "; stopped early: " in text and "SIMULATED" not in text


def test_describe_poll_watch_says_official(tmp_path):
    case = Case(SessionStore(tmp_path).open("c"))
    ev = case.append_watch({"id": "002", "mode": "poll", "sim": False, "seconds": 20.0, "end_reason": "time",
                            "notes": [], "capture_id": None,
                            "summary": {"samples": 180,
                                        "rpm": {"source": "010C (official)", "answered": 180, "max": 0.0,
                                                "median_nonzero": None, "first_nonzero_s": None},
                                        "voltage": {"min": 9.8, "max": 12.1}}})
    assert describe(ev) == ("OBD watch 002 (poll, 20.0 s): 180 RPM samples (010C, official), max 0 rpm; "
                            "adapter voltage 9.8–12.1 V")
    s = build_summary(case)
    assert s.watches == [{"id": "002", "ts": ev.ts, "text": describe(ev), "sim": False, "capture_id": None}]
    assert s.timeline[-1]["line"] == describe(ev)


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
    assert [c["code"] for c in s.codes] == ["P2119", "P0335"]      # every code read, in order of first read


def test_not_yet_tried_shrinks_as_steps_get_data(case):
    from crank.procedure import step_by_id
    case.log_guided_step(step_by_id("static"), {"battery_v": 12.6, "gnd_batt_block_ohm": 0.4,
                                                "gnd_block_chassis_ohm": 0.3, "gnd_batt_pcm_ohm": 0.5})
    _capture(case, "healthy", "baseline")
    left = build_summary(case).not_tried
    assert all("Static baseline" not in t for t in left)
    assert any("Key on, engine off" in t for t in left)
    assert any("Baseline cranking" in t for t in left)             # the only capture is simulated


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


# --- final-review fixes
def test_measurements_are_kept_per_name_where_and_conditions(case):
    case.append("measurement", "", {"name": "Battery", "value": 12.6, "unit": "V", "conditions": "key off"})
    case.append("measurement", "", {"name": "Battery", "value": 9.8, "unit": "V", "conditions": "cranking"})
    s = build_summary(case)
    assert sorted((m["conditions"], m["value"]) for m in s.measurements) == [("cranking", 9.8), ("key off", 12.6)]


def test_codes_from_all_reads_keep_their_status(case):
    case.append("code_read", "", {"codes": [{"code": "P2119", "status": "history"}]})
    case.append("code_read", "", {"codes": [{"code": "P0335", "status": "pending"},
                                            {"code": "P2119", "status": "current"}]})
    codes = {c["code"]: c["status"] for c in build_summary(case).codes}
    assert codes == {"P2119": "current", "P0335": "pending"}


def test_a_simulated_capture_does_not_count_as_the_baseline(case):
    _capture(case, "healthy", "sim run")
    assert any("Baseline cranking" in t for t in build_summary(case).not_tried)
    real = generate("healthy", seconds=1.0)
    cid = case.session.add_capture(real, "real")
    case.append("capture", "real", {"snapshot": None, "sim": False}, {"capture": cid})
    assert all("Baseline cranking" not in t for t in build_summary(case).not_tried)


def test_inferred_pairs_are_flagged_and_explicit_ones_are_not(case):
    a = _capture(case, "ref_sag", "A")
    case.append("change", "inferred change")
    b = _capture(case, "healthy", "B")
    case.append("change", "explicit change", {}, {"before": a, "after": b})
    pairs = {p["change"]: p["inferred"] for p in build_summary(case).before_after}
    assert pairs == {"inferred change": True, "explicit change": False}


def test_a_partial_link_keeps_the_given_side_and_infers_the_other(case):
    a = _capture(case, "ref_sag", "A")
    b = _capture(case, "healthy", "B")
    case.append("change", "swapped sensor", {}, {"before": a})
    c = _capture(case, "ground_lift", "C")
    pair = build_summary(case).before_after[0]
    assert (pair["before"]["id"], pair["after"]["id"], pair["inferred"]) == (a, c, True) and b


def test_attaching_a_capture_does_not_change_the_pairing(case):
    a = _capture(case, "ref_sag", "A")
    case.append("change", "swapped sensor", {}, {"capture": a})
    c = _capture(case, "healthy", "C")
    pair = build_summary(case).before_after[0]
    assert (pair["before"]["id"], pair["after"]["id"]) == (a, c)
