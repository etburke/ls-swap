"""Synthetic cranking captures for named fault scenarios."""
from __future__ import annotations

import numpy as np

from .model import CaptureData, default_channels

SCENARIOS = [
    "healthy", "no_signal", "ground_lift", "ref_sag", "runt_pulses",
    "missing_teeth", "missing_teeth_no_gap", "harness_fault", "late_start",
]

# Finding codes each scenario must produce (checked by tests/test_analysis.py).
EXPECTED_CODES = {
    "healthy": {"signal_clean"},
    "no_signal": {"no_signal"},
    "ground_lift": {"ground_lift"},
    "ref_sag": {"ref_sag"},
    "runt_pulses": {"unexplained_dropouts"},
    "missing_teeth": {"unexplained_dropouts"},
    "missing_teeth_no_gap": {"no_sync_gap"},
    "harness_fault": {"harness_mismatch"},
    "late_start": {"signal_clean"},
}


def generate(scenario: str, seconds: float = 6.0, fs: int = 50_000,
             seed: int = 1, teeth: int = 58, pretrig: float = 0.05) -> CaptureData:
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}; choose from {SCENARIOS}")
    rng = np.random.default_rng(seed)
    n = int(seconds * fs)
    t = -pretrig * seconds + np.arange(n) / fs
    t_start = 2.0 if scenario == "late_start" else 0.0
    on = t >= t_start

    # starter speed: fast spin-up, ~200 RPM with compression ripple
    ramp = np.clip((t - t_start) / 0.2, 0, 1)
    rps = 3.3 * ramp * (1 + 0.15 * np.sin(2 * np.pi * 13 * (t - t_start)))
    slot_f = np.cumsum(rps) / fs * 60 + 0.75      # start low, mid-slot
    idx = np.floor(slot_f).astype(int)
    phase = slot_f - idx
    slot = idx % 60

    tooth_slot = np.ones_like(slot, bool) if scenario == "missing_teeth_no_gap" else slot < teeth
    tooth = tooth_slot & (phase < 0.5)

    if scenario == "missing_teeth":
        rnd = rng.random(idx.max() + 1)
        tooth &= ~((slot % 3 == 1) & (slot < teeth) & (rnd[idx] < 0.15))
    amp = np.ones(n)
    if scenario == "runt_pulses":
        rnd = rng.random(idx.max() + 1)
        amp = np.where((slot < teeth) & (rnd[idx] < 0.08), 0.6, 1.0)

    load = np.where(on, 0.6 + 0.4 * np.exp(-np.clip(t - t_start, 0, None) / 0.4), 0.0)
    ref = 5.0 - 0.05 * load
    gnd = 0.03 * load
    if scenario == "ref_sag":
        ref = 5.0 - 1.8 * load
    if scenario == "ground_lift":
        gnd = 1.5 * load
    ref = ref + rng.normal(0, 0.004, n)
    gnd = gnd + rng.normal(0, 0.003, n)

    level = np.minimum(ref, 5.0) * amp
    ch1 = tooth * level + rng.normal(0, 0.03, n)
    ch2 = tooth * level + rng.normal(0, 0.03, n)
    if scenario == "harness_fault":
        ch1 = tooth * level * 0.55 + rng.normal(0, 0.03, n)
    if scenario == "no_signal":
        ch1 = rng.normal(0.02, 0.03, n)
        ch2 = rng.normal(0.02, 0.03, n)

    labels = {c.n: c.label for c in default_channels()}
    return CaptureData(t, {1: ch1, 2: ch2, 3: ref, 4: gnd}, labels,
                       {"sim": True, "scenario": scenario, "fs": fs, "seed": seed})
