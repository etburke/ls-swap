import json
import threading

import pytest

from crank.case import Case, Event, Vehicle, capture_snapshot, load_events, suggest_hypotheses
from crank.procedure import step_by_id
from crank.store import SessionStore


@pytest.fixture
def case(tmp_path):
    return Case(SessionStore(tmp_path).open("c"))


def test_append_and_read_back_in_order_with_unique_ids(case):
    a = case.append("symptom", "no RPM while cranking")
    b = case.append("observation", "smells like fuel")
    assert (a.id, b.id) == ("e0001", "e0002")
    assert [e.text for e in case.events()] == ["no RPM while cranking", "smells like fuel"]
    assert a.ts and case.events()[0].type == "symptom"


def test_damaged_lines_are_skipped_and_counted(case):
    case.append("symptom", "one")
    with open(case.path / "events.jsonl", "a") as f:
        f.write("{truncated\n[1,2]\n\n")
    case.append("symptom", "two")
    assert [e.text for e in case.events()] == ["one", "two"] and case.skipped == 2


def test_concurrent_appends_do_not_interleave_or_duplicate_ids(case):
    def work():
        for _ in range(25):
            case.append("observation", "x")
    ts = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    events, skipped = load_events(case.path / "events.jsonl")
    assert skipped == 0 and len(events) == 100 and len({e.id for e in events}) == 100


def test_unknown_types_and_extra_fields_load_without_error(case):
    (case.path / "events.jsonl").write_text(
        json.dumps({"id": "e0001", "ts": "2026-01-01T00:00:00", "type": "future_thing",
                    "text": "x", "mystery": 1}) + "\n")
    e = case.events()[0]
    assert e.type == "future_thing" and case.skipped == 0


@pytest.mark.parametrize("type_,text,data,msg", [
    ("bogus", "x", {}, "unknown event type"),
    ("symptom", "  ", {}, "needs some text"),
    ("measurement", "", {"name": "V", "value": "abc", "unit": "V"}, "numeric value"),
    ("measurement", "", {"name": "V", "value": 1}, "needs a unit"),
    ("hypothesis", "", {"hid": "h1", "title": "t", "status": "ruled_out"}, "needs a reason"),
    ("hypothesis", "", {"hid": "h1", "title": "t", "status": "weird"}, "unknown hypothesis status"),
    ("code_read", "", {"codes": []}, "at least one code"),
])
def test_invalid_entries_are_rejected_and_nothing_is_written(case, type_, text, data, msg):
    with pytest.raises(ValueError, match=msg):
        case.append(type_, text, data)
    assert case.events() == []


def test_hypothesis_latest_status_wins(case):
    h = case.set_hypothesis(None, "Bad PCM")
    case.set_hypothesis(h.data["hid"], "Bad PCM", "ruled_out", "second PCM behaved the same")
    latest = case.hypotheses()[h.data["hid"]]
    assert latest.data["status"] == "ruled_out" and "second PCM" in latest.data["reason"]
    assert len(case.hypotheses()) == 1


def test_vehicle_roundtrip_and_corrupt_file(case):
    assert case.vehicle().empty and case.vehicle().title == "Vehicle not recorded"
    case.set_vehicle(Vehicle(year="1987", make="Toyota", model="FJ60", problem="0 RPM"))
    assert case.vehicle().title == "1987 Toyota FJ60"
    (case.path / "vehicle.json").write_text("garbage")
    assert case.vehicle().empty and (case.path / "vehicle.json.corrupt").exists()
    (case.path / "vehicle.json").write_text("[1]")
    assert case.vehicle().empty


def test_legacy_session_opens_as_case_without_rewriting_files(tmp_path):
    from crank.sim import generate
    ses = SessionStore(tmp_path).open("old")
    ses.add_capture(generate("healthy", seconds=1.0), "baseline")
    ses.set_reading("static", {"battery_v": 12.6, "gnd_batt_block_ohm": 0.4,
                               "gnd_block_chassis_ohm": 0.3, "gnd_batt_pcm_ohm": 0.5})
    ses.set_notes("grounds are new")
    before = sorted(p.name for p in ses.path.rglob("*") if p.is_file())
    ev = Case(ses).all_events()
    kinds = sorted(e.type for e in ev)
    assert kinds.count("capture") == 1 and kinds.count("measurement") == 4 and kinds.count("observation") == 1
    assert all(e.synth for e in ev)
    assert sorted(p.name for p in ses.path.rglob("*") if p.is_file()) == before    # nothing written


def test_real_event_replaces_its_synthesized_twin(case):
    from crank.sim import generate
    cid = case.session.add_capture(generate("healthy", seconds=1.0), "baseline")
    assert case.all_events()[0].synth
    case.append_capture(cid, "baseline", None, True)
    evs = case.all_events()
    assert len(evs) == 1 and not evs[0].synth


def test_guided_step_becomes_measurements_and_observations(case):
    case.log_guided_step(step_by_id("static"), {"battery_v": 12.6, "gnd_batt_block_ohm": 0.4,
                                                "gnd_block_chassis_ohm": 0.3, "gnd_batt_pcm_ohm": 0.5})
    case.log_guided_step(step_by_id("mechanical"), {"sensor_mount_ok": True, "reluctor_ok": False,
                                                    "balancer_ok": True, "notes": "tooth damage?"})
    ev = case.events()
    assert [e.type for e in ev].count("measurement") == 4
    texts = [e.text for e in ev if e.type == "observation"]
    assert "Reluctor wheel clean and undamaged: no" in texts and "Notes: tooth damage?" in texts
    assert all(e.refs.get("step") for e in ev)
    assert len(case.all_events()) == len(ev)             # saved steps are not duplicated by legacy synthesis


def test_capture_snapshot_freezes_verdict():
    from crank.analysis import analyze_capture
    from crank.sim import generate
    snap = capture_snapshot(analyze_capture(generate("ref_sag")))
    assert snap["verdict"][0]["code"] == "ref_sag" and snap["ref_sag_mv"] > 1000
    assert capture_snapshot(None) is None


def test_suggestions_skip_existing_and_unknown_codes():
    s = suggest_hypotheses(["ref_sag", "mystery", "ground_lift"], ["5V reference circuit and anything sharing it"])
    assert s == ["PCM ground path under load"]
