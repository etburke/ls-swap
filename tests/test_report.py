import numpy as np

from crank.analysis import analyze_capture
from crank.report import build_figure, build_overlay, decimate, text_report, to_csv
from crank.sim import generate


def test_decimate_keeps_a_single_sample_runt():
    t = np.arange(200_000) / 1e4
    v = np.zeros(t.size)
    v[123_457] = 5.0
    td, vd = decimate(t, v, max_points=2000)
    assert len(vd) <= 2000 and vd.max() == 5.0


def test_decimate_short_input_unchanged():
    t, v = np.arange(10.0), np.arange(10.0)
    assert decimate(t, v)[0] is t


def test_text_report_mentions_verdict_and_channels():
    d = generate("ref_sag")
    out = text_report(analyze_capture(d))
    assert "[FAIL] 5V reference sags" in out and "Crank @ PCM" in out


def test_text_report_for_dead_signal():
    out = text_report(analyze_capture(generate("no_signal")))
    assert "[FAIL] No usable crank signal" in out and "no usable signal" in out


def test_csv_has_header_and_all_rows():
    d = generate("healthy", seconds=1.0)
    lines = to_csv(d).decode().splitlines()
    assert lines[0].startswith("t_s,") and len(lines) == len(d.t) + 1


def test_figures_build_for_full_and_zoomed_window_with_many_events():
    d = generate("ref_sag")                     # ~1000 events: must stay fast
    a = analyze_capture(d)
    assert len(build_figure(d, a).data) >= 5
    assert len(build_figure(d, a, 0.0, 0.05).data) >= 5
    assert len(build_overlay(d, generate("healthy"), 1).data) == 2
