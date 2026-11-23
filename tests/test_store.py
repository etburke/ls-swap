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


def test_delete_session_removes_the_folder_and_nothing_else(tmp_path):
    store = SessionStore(tmp_path)
    store.open("keep").add_capture(generate("healthy", seconds=1), "ok")
    store.open("drop").add_capture(generate("healthy", seconds=1), "ok")
    store.delete_session("drop")
    assert store.list_sessions() == ["keep"]
    assert store.open("keep").list_captures()


def test_delete_session_rejects_an_unknown_name_and_never_leaves_the_root(tmp_path):
    store = SessionStore(tmp_path / "root")
    (tmp_path / "outside").mkdir()
    with pytest.raises(KeyError):
        store.delete_session("nope")
    with pytest.raises(KeyError):
        store.delete_session("../outside")                # sanitised to a name that does not exist
    assert (tmp_path / "outside").exists()


def test_new_watch_allocates_sequential_ids_and_writes_as_it_goes(tmp_path):
    ses = SessionStore(tmp_path).open("s")
    w = ses.new_watch()
    assert w.id == "001" and (ses.path / "watches" / "001.jsonl").exists()
    w.record({"t": 1.0, "raw": "0C98000C800000000000"})
    assert '"raw": "0C98000C800000000000"' in (ses.path / "watches" / "001.jsonl").read_text()
    assert ses.list_watches() == []                     # not finished yet
    w.finish({"id": "001", "mode": "raw"})
    w2 = ses.new_watch()
    assert w2.id == "002"
    w2.finish({"id": "002", "mode": "poll"})
    assert [m["id"] for m in ses.list_watches()] == ["001", "002"]
    meta, rows = ses.load_watch("001")
    assert meta["mode"] == "raw" and rows == [{"t": 1.0, "raw": "0C98000C800000000000"}]


def test_discard_removes_the_watch(tmp_path):
    ses = SessionStore(tmp_path).open("s")
    w = ses.new_watch()
    w.record({"t": 1.0, "raw": "x"})
    w.discard()
    assert list((ses.path / "watches").iterdir()) == [] and ses.list_watches() == []
    assert ses.new_watch().id == "001"


def test_link_watch_cross_references_both_metas(tmp_path):
    ses = SessionStore(tmp_path).open("s")
    cid = ses.add_capture(generate("healthy", seconds=1), "crank")
    w = ses.new_watch()
    w.finish({"id": w.id, "capture_id": None})
    ses.link_watch(w.id, cid)
    assert ses.load_watch(w.id)[0]["capture_id"] == cid
    assert ses.list_captures()[0]["watch_id"] == w.id


def test_load_watch_unknown_raises_keyerror(tmp_path):
    ses = SessionStore(tmp_path).open("s")
    with pytest.raises(KeyError):
        ses.load_watch("001")
    with pytest.raises(KeyError):
        ses.load_watch("../x")


def test_new_capture_saves_roles(tmp_path):
    ses = SessionStore(tmp_path).open("s")
    cid = ses.add_capture(generate("healthy"))
    got = ses.load_capture(cid)
    assert got.meta["roles"]["2"] == {"role": "crank_sensor", "num": None} and got.meta["roles_source"] == "saved"


def test_legacy_capture_without_roles(tmp_path):
    data = generate("healthy")
    data.meta.pop("roles", None)
    ses = SessionStore(tmp_path).open("s")
    cid = ses.add_capture(data)
    got = ses.load_capture(cid)
    assert got.meta["roles_source"] == "legacy" and got.meta["roles"]["3"]["role"] == "crank_ref"


def test_correct_roles_survives_reload_and_json_missing(tmp_path):
    ses = SessionStore(tmp_path).open("s")
    cid = ses.add_capture(generate("healthy"))
    (ses.path / "captures" / f"{cid}.json").unlink()
    ses.set_capture_roles(cid, {2: ("cam", None), 3: ("est", 1)})
    got = SessionStore(tmp_path).open("s").load_capture(cid)
    assert got.meta["roles_source"] == "corrected"
    assert got.meta["roles"]["2"]["role"] == "cam" and got.meta["roles"]["3"] == {"role": "est", "num": 1}
    assert got.meta["roles"]["1"]["role"] == "crank_ecm"
    assert [c["roles_corrected"] for c in SessionStore(tmp_path).open("s").list_captures()] == [True]


def test_correct_roles_refuses_missing_channel_and_bad_role(tmp_path):
    ses = SessionStore(tmp_path).open("s")
    cid = ses.add_capture(generate("healthy"))
    with pytest.raises(ValueError, match="CH5"):
        ses.set_capture_roles(cid, {5: ("cam", None)})
    with pytest.raises(ValueError):
        ses.set_capture_roles(cid, {2: ("cam", 3)})
