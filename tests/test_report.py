import numpy as np

from crank.analysis import analyze_capture
from crank.report import build_figure, build_overlay, decimate, text_report, to_csv
from crank.sim import generate


def test_report_has_an_obd_watches_section_in_markdown_and_html(tmp_path):
    from crank.case import Case
    from crank.render import to_html, to_markdown
    from crank.store import SessionStore
    from crank.summary import build_summary
    case = Case(SessionStore(tmp_path).open("c"))
    case.append_watch({"id": "001", "mode": "raw", "sim": True, "seconds": 18.4, "end_reason": "time",
                       "notes": [], "capture_id": "003",
                       "summary": {"lines": 10, "frames": 10, "ids": {"0C9": {"count": 10, "hz": 1.0}},
                                   "engine_speed": {"max": 200.0, "best_effort": True}}})
    s = build_summary(case)
    md, html = to_markdown(s), to_html(s)
    for out in (md, html):
        assert "OBD watches" in out and "best-effort decoding" in out
        assert "OBD watch 001" in out and "capture 003" in out
    assert "## OBD watches" in md and "<h2>OBD watches</h2>" in html
    empty = Case(SessionStore(tmp_path).open("empty"))
    empty.append("symptom", "no RPM")
    assert "OBD watches" not in to_markdown(build_summary(empty))


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


def test_figure_rows_follow_roles():
    from crank.analysis import analyze_capture
    from crank.report import build_figure
    from crank.sim import generate
    d = generate("healthy")
    d.meta["roles"] = {"1": {"role": "crank_ecm", "num": None}, "2": {"role": "cam", "num": None},
                       "3": {"role": "est", "num": 1}, "4": {"role": "injector", "num": 1}}
    titles = [a.text for a in build_figure(d, analyze_capture(d)).layout.annotations]
    assert not any("ECM sees" in t or "CH1-CH4" in t for t in titles)
    assert any("Cam signal" in t for t in titles) and any("Ignition control (EST) 1" in t for t in titles)


def test_figure_keeps_the_ecm_relative_row_for_the_classic_layout():
    from crank.analysis import analyze_capture
    from crank.report import build_figure
    from crank.sim import generate
    d = generate("ground_lift")
    titles = [a.text for a in build_figure(d, analyze_capture(d)).layout.annotations]
    assert any("Crank as the ECM sees it (CH1-CH4)" in t for t in titles) and len(titles) == 5
