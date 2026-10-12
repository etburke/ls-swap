import json

import numpy as np

from crank.model import CaptureData, CaptureSettings, Thresholds, default_channels


def test_defaults_match_the_wiring_plan():
    ch = default_channels()
    assert [c.n for c in ch] == [1, 2, 3, 4]
    assert ch[2].offset == -5.0 and CaptureSettings().depth == "10M"
    assert Thresholds().teeth == 58


def test_capture_roundtrip_is_atomic_and_lossless(tmp_path):
    t = np.arange(10) / 100
    d = CaptureData(t, {1: np.linspace(0, 5, 10), 3: np.full(10, 5.0)}, {1: "a", 3: "b"}, {"x": 1})
    p = str(tmp_path / "c.npz")
    d.save(p)
    assert not (tmp_path / "c.npz.tmp").exists()
    e = CaptureData.load(p)
    assert np.allclose(e.t, t) and np.allclose(e.channels[1], d.channels[1])
    assert e.labels == {1: "a", 3: "b"} and e.meta == {"x": 1}


def test_loads_legacy_npz_from_original_script(tmp_path):
    p = tmp_path / "old.npz"
    np.savez_compressed(p, t=np.arange(5.0), ch1=np.zeros(5), ch2=np.zeros(5),
                        meta=json.dumps({"1": "Crank @ PCM", "2": "Crank @ sensor"}))
    e = CaptureData.load(str(p))
    assert e.labels == {1: "Crank @ PCM", 2: "Crank @ sensor"} and set(e.channels) == {1, 2}
