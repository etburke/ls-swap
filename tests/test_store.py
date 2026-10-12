import pytest

from crank.sim import generate
from crank.store import SessionStore


def test_store_roundtrip_and_numbering(tmp_path):
    ses = SessionStore(tmp_path).open("2026-10-20 baseline!")
    assert ses.name == "2026-10-20_baseline"
    a = ses.add_capture(generate("healthy", seconds=1), "baseline", "stock grounds")
    b = ses.add_capture(generate("ref_sag", seconds=1), "after reground")
    assert (a, b) == ("001", "002")
    assert [c["label"] for c in ses.list_captures()] == ["baseline", "after reground"]
    assert ses.load_capture("002").meta["scenario"] == "ref_sag"
    ses.set_reading("static", {"battery_v": 12.6}); ses.set_notes("hello")
    again = SessionStore(tmp_path).open("2026-10-20 baseline!")
    assert again.readings()["static"]["battery_v"] == 12.6 and again.notes() == "hello"
    assert SessionStore(tmp_path).list_sessions() == ["2026-10-20_baseline"]


def test_store_survives_corrupt_files(tmp_path):
    ses = SessionStore(tmp_path).open("s")
    ses.add_capture(generate("healthy", seconds=1), "ok")
    (ses.path / "captures" / "001.json").write_text("{not json")
    (ses.path / "readings.json").write_text("garbage")
    assert ses.list_captures()[0]["id"] == "001"
    assert ses.readings() == {}
    assert (ses.path / "readings.json.corrupt").exists()


def test_store_rejects_empty_session_name(tmp_path):
    with pytest.raises(ValueError):
        SessionStore(tmp_path).open("  !! ")
