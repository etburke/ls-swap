import numpy as np
import pytest

from crank.analysis import analyze_capture
from crank.model import CaptureData, Thresholds
from crank.sim import EXPECTED_CODES, SCENARIOS, generate


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_scenario_produces_expected_findings(scenario, seed):
    a = analyze_capture(generate(scenario, seed=seed))
    assert a.codes == EXPECTED_CODES[scenario]


def test_healthy_counts_58_edges_per_revolution():
    r = analyze_capture(generate("healthy")).reports["ch1"]
    assert r.usable and r.revs >= 10 and r.revs_good == r.revs
    assert 150 < r.rpm_median < 250


def test_no_signal_does_not_crash_and_reports_zero_edges():
    a = analyze_capture(generate("no_signal"))
    assert a.reports["ch1"].n_edges == 0 and not a.reports["ch1"].usable


def test_ground_lift_visible_only_in_pcm_relative_trace():
    a = analyze_capture(generate("ground_lift"))
    assert a.reports["ch1"].anomalies == 0
    assert a.reports["ch1_rel"].anomalies > 0
    assert a.supply["gnd_dev_mv"] > 1000


def test_ref_sag_events_align_with_supply_disturbance():
    a = analyze_capture(generate("ref_sag"))
    assert a.correlations and sum(c["aligned"] for c in a.correlations) / len(a.correlations) > 0.9


def test_runt_events_do_not_align_when_supply_steady():
    a = analyze_capture(generate("runt_pulses"))
    assert a.correlations and not any(c["aligned"] for c in a.correlations)


def test_harness_fault_compares_edges_between_channels():
    a = analyze_capture(generate("harness_fault"))
    assert a.reports["ch2"].usable and not a.reports["ch1"].usable


def test_only_ch2_present_still_works():
    d = generate("healthy")
    d.channels = {2: d.channels[2]}
    a = analyze_capture(d)
    assert a.codes == {"signal_clean"} and a.compare is None


def test_no_crank_channels_is_insufficient_data():
    d = generate("healthy")
    d.channels = {3: d.channels[3], 4: d.channels[4]}
    assert analyze_capture(d).codes == {"insufficient_data"}


# --- Review Focus inputs
def test_constant_signal_stuck_high():
    t = np.arange(0, 1, 1e-4)
    d = CaptureData(t, {1: np.full_like(t, 5.0), 2: np.full_like(t, 5.0)}, {1: "a", 2: "b"})
    assert analyze_capture(d).codes == {"no_signal"}


def test_very_short_capture():
    d = generate("healthy", seconds=0.05)
    assert analyze_capture(d).codes <= {"no_signal", "signal_clean", "no_sync_gap"}


def test_nan_samples_do_not_crash():
    d = generate("healthy", seconds=1.0)
    d.channels[1] = d.channels[1].copy()
    d.channels[1][100:110] = np.nan
    analyze_capture(d)


def test_custom_thresholds_for_different_wheel():
    th = Thresholds(teeth=24)
    a = analyze_capture(generate("healthy"), th)
    assert a.reports["ch1"].revs_good == 0          # 58x signal never matches 24 edges/rev


# --- final-review fixes
def _wipe_tooth(d, edge_no=300, width=200):
    for ch in (1, 2):
        v = d.channels[ch]
        rises = np.flatnonzero((v[1:] > 2.5) & (v[:-1] <= 2.5))
        v[rises[edge_no]:rises[edge_no] + width] = 0.0


@pytest.mark.parametrize("n_teeth", [1, 3])
def test_a_few_dropped_teeth_are_not_reported_clean(n_teeth):
    d = generate("healthy")
    for k in range(n_teeth):
        _wipe_tooth(d, 300 + 40 * k)
    a = analyze_capture(d)
    assert "signal_clean" not in a.codes and "unexplained_dropouts" in a.codes


def test_nan_in_supply_channel_does_not_hide_ref_sag():
    d = generate("ref_sag")
    d.channels[3] = d.channels[3].copy()
    d.channels[3][150_000] = np.nan
    a = analyze_capture(d)
    assert "ref_sag" in a.codes and "bad_samples" in a.codes
    assert "unexplained_dropouts" not in a.codes


def test_nan_samples_prevent_a_clean_verdict():
    d = generate("healthy")
    d.channels[4] = d.channels[4].copy()
    d.channels[4][100_000] = np.nan
    assert analyze_capture(d).codes == {"bad_samples"}
