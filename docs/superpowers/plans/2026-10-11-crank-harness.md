# Crank Test Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Streamlit crank-signal test harness (capture, analysis, verdict, guided procedure, compare) for the LS-swap 0-RPM diagnosis, fully testable without the scope.

**Architecture:** Pure-function analysis and a scenario simulator sit behind a `ScopeInterface` (`RigolScope` for the real DHO804, `SimScope` for tests and offline use). A `CaptureJob` thread drives configure/arm/wait/download/save/analyze. A file-based session store keeps captures and manual readings. `app.py` is UI only.

**Tech Stack:** Python 3.11, numpy, Streamlit, Plotly, pyvisa + pyvisa-py + pyusb, pytest.

**Spec:** `docs/superpowers/specs/2026-10-11-crank-harness-design.md`

**Reference implementation:** Every code block below was prototyped in a scratch directory and passed 78 tests there (analysis, simulator, capture flow, storage, report, Streamlit `AppTest`, CLI). Still follow the red/green steps in each task: they confirm the code in this repo, and the failing run proves each test can fail.

## Global Constraints

- Python 3.11; dependencies exactly those in `requirements.txt` (numpy, streamlit, plotly, pyvisa, pyvisa-py, pyusb, pytest).
- No test may need the scope, a network, or a display. All scope-facing logic is tested against `SimScope` or a fake VISA object.
- Default channel mapping: CH1 crank at PCM pin, CH2 crank at sensor connector, CH3 5V reference, CH4 PCM low reference vs battery negative. All probe grounds to battery negative.
- Crank wheel is 58x: 58 rising edges per revolution, a 3x-period sync gap (2 missing teeth). Default thresholds: hi 3.5 V, lo 1.5 V, runt-high 4.0 V, runt-low 1.0 V.
- Memory depth values must be ones Rigol offers (default `10M`, never `5M`).
- SCPI strings are unverified on real hardware; every write is followed by an error-queue check. Critical settings (`:ACQuire:MDEPth`, `:TIMebase`, `:TRIGger`, `:WAVeform:MODE`, `:SINGle`) raise `ScopeError` when rejected; others log a warning.
- Captures are saved to disk before analysis runs. File writes are atomic (temp file then `os.replace`).
- The verdict reports a likely area and its evidence, never a certain diagnosis, and says "insufficient data" instead of guessing.
- Commit messages end with the trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

- A channel stuck at a constant level (flat high or flat low): must report `no_signal`, not crash. Pinned in Task 3.
- A capture shorter than one crank revolution: must not crash and must not claim a clean verdict from nothing. Pinned in Task 3.
- NaN samples from a bad download in a crank channel: analysis must still return a result. Pinned in Task 3.
- Odd or empty session names, and half-written or corrupt session files: names are sanitized, empty ones rejected, corrupt files set aside and the session still opens. Pinned in Task 6.
- The scope accepting fewer points or a smaller memory depth than requested: surfaced as a log line and as a red line in the self-check instead of silently producing a coarse capture. Pinned in Task 4.

## Deviations from the spec's file list

The spec lists `scope.py, sim.py, capture.py, analysis.py, store.py, procedure.py, app.py`. This plan adds `crank/model.py` (shared dataclasses, so modules do not import each other cyclically) and `crank/report.py` (text report and Plotly figures, keeping `analysis.py` free of plotting). The command-line tool drops its matplotlib PNG output; plots now live in the app. The scope self-check required by the spec is `RigolScope.self_check` plus a sidebar button.

---

## File Structure

```
requirements.txt  .gitignore  README.md
crank/__init__.py  model.py  sim.py  analysis.py  scope.py  capture.py  store.py  procedure.py  report.py
app.py                      # Streamlit UI only
crank_capture.py            # CLI (replaces the original single-file script)
tests/__init__.py  test_model.py  test_sim.py  test_analysis.py  test_scope.py  test_capture.py
                   test_store.py  test_procedure.py  test_report.py  test_app.py  test_cli.py
```

---

### Task 1: Project scaffolding and data model

**Files:**
- Create: `requirements.txt`, `.gitignore`, `crank/__init__.py`, `tests/__init__.py`, `crank/model.py`
- Test: `tests/test_model.py`

**Interfaces:**
- Produces: `ChannelConfig(n, label, scale, offset=0.0, enabled=True)`; `default_channels() -> list[ChannelConfig]`; `CaptureSettings(seconds=6.0, pretrig=0.05, depth="10M", trig_level=2.5, wait=120.0, channels=default_channels())`; `Thresholds(...)` (fields in the code below); `CaptureData(t, channels: dict[int, ndarray], labels: dict[int, str], meta: dict)` with `.save(path)` (atomic) and `CaptureData.load(path)` (also reads files written by the original `crank_capture.py`).

- [ ] **Step 1: Create the environment and scaffolding files**

```bash
cd /Users/gnomonic/projects/ls-swap
python3 -m venv .venv
mkdir -p crank tests
touch crank/__init__.py tests/__init__.py
```

`requirements.txt`:

```
numpy>=1.26
streamlit>=1.40
plotly>=5.20
pyvisa>=1.13
pyvisa-py>=0.7
pyusb>=1.2
pytest>=8
```

`.gitignore`:

```
.venv/
__pycache__/
.pytest_cache/
sessions/
*.npz
```

```bash
.venv/bin/pip install -r requirements.txt     # macOS: brew install libusb for USB access later
```

- [ ] **Step 2: Write the failing test**

`tests/test_model.py`:

```python
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_model.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crank.model'`

- [ ] **Step 4: Write the implementation**

`crank/model.py`:

```python
"""Shared data types: settings, thresholds, and the CaptureData container."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import numpy as np


@dataclass
class ChannelConfig:
    n: int
    label: str
    scale: float            # V/div
    offset: float = 0.0     # V
    enabled: bool = True


def default_channels() -> list[ChannelConfig]:
    return [
        ChannelConfig(1, "Crank @ PCM", 2.0, 0.0),
        ChannelConfig(2, "Crank @ sensor", 2.0, 0.0),
        ChannelConfig(3, "5V ref @ sensor", 0.5, -5.0),
        ChannelConfig(4, "PCM low ref vs batt -", 0.2, 0.0),
    ]


@dataclass
class CaptureSettings:
    seconds: float = 6.0
    pretrig: float = 0.05
    depth: str = "10M"
    trig_level: float = 2.5
    wait: float = 120.0
    channels: list[ChannelConfig] = field(default_factory=default_channels)


@dataclass
class Thresholds:
    hi: float = 3.5               # Schmitt high threshold (V)
    lo: float = 1.5               # Schmitt low threshold (V)
    runt_hi: float = 4.0          # a high pulse peaking below this is a runt
    runt_lo: float = 1.0          # a low pulse bottoming above this is weak
    teeth: int = 58               # rising edges per crank revolution
    min_edges: int = 12           # fewer rising edges than this = no usable signal
    skip_first_edges: int = 4     # ignore the starter spin-up transient
    ref_nominal: float = 5.0
    ref_sag_mv: float = 300.0     # 5V-ref sag that counts as a finding
    ground_mv: float = 150.0      # low-ref deviation that counts as a finding
    event_window_s: float = 0.0005


@dataclass
class CaptureData:
    t: np.ndarray                         # seconds relative to trigger
    channels: dict[int, np.ndarray]       # channel number -> volts
    labels: dict[int, str]
    meta: dict = field(default_factory=dict)

    def save(self, path: str) -> None:
        """Atomic write: temp file then rename."""
        tmp = path + ".tmp"
        arrays = {f"ch{n}": v for n, v in self.channels.items()}
        with open(tmp, "wb") as f:
            np.savez_compressed(
                f, t=self.t,
                meta=json.dumps(self.meta),
                labels=json.dumps({str(k): v for k, v in self.labels.items()}),
                **arrays)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str) -> "CaptureData":
        with np.load(path, allow_pickle=False) as d:
            t = d["t"]
            channels = {int(k[2:]): d[k] for k in d.files
                        if k.startswith("ch") and k[2:].isdigit()}
            if "labels" in d.files:
                labels = {int(k): v for k, v in json.loads(str(d["labels"])).items()}
                meta = json.loads(str(d["meta"]))
            else:  # files written by the original crank_capture.py: meta held the labels
                labels = {int(k): v for k, v in json.loads(str(d["meta"])).items()}
                meta = {}
        return cls(t, channels, labels, meta)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_model.py -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add requirements.txt .gitignore crank/__init__.py crank/model.py tests/__init__.py tests/test_model.py
git commit -m "feat: project scaffolding and CaptureData model" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Scenario simulator

**Files:**
- Create: `crank/sim.py`
- Test: `tests/test_sim.py`

**Interfaces:**
- Consumes: `CaptureData`, `default_channels` from `crank.model`.
- Produces: `SCENARIOS: list[str]` (9 names); `EXPECTED_CODES: dict[str, set[str]]` (finding codes each scenario must yield); `generate(scenario, seconds=6.0, fs=50_000, seed=1, teeth=58, pretrig=0.05) -> CaptureData` with channels `{1,2,3,4}` and `meta["scenario"]`.

- [ ] **Step 1: Write the failing test**

`tests/test_sim.py`:

```python
import numpy as np
import pytest

from crank.sim import SCENARIOS, generate


def test_every_scenario_generates_four_channels():
    for s in SCENARIOS:
        d = generate(s)
        assert set(d.channels) == {1, 2, 3, 4}
        assert all(len(v) == len(d.t) for v in d.channels.values())
        assert d.meta["scenario"] == s


def test_unknown_scenario_rejected():
    with pytest.raises(ValueError):
        generate("bogus")


def test_same_seed_is_deterministic():
    a, b = generate("runt_pulses", seed=3), generate("runt_pulses", seed=3)
    assert np.array_equal(a.channels[1], b.channels[1])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_sim.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crank.sim'`

- [ ] **Step 3: Write the implementation**

`crank/sim.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_sim.py tests/test_model.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add crank/sim.py tests/test_sim.py
git commit -m "feat: scenario simulator" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Analysis and verdict

**Files:**
- Create: `crank/analysis.py`
- Test: `tests/test_analysis.py`

**Interfaces:**
- Consumes: `CaptureData`, `Thresholds` (Task 1); `generate`, `SCENARIOS`, `EXPECTED_CODES` (Task 2).
- Produces: `analyze_capture(data, th=None) -> Analysis`. `Analysis` has `reports: dict[str, SignalReport]` (keys `ch1`, `ch2`, `ch1_rel` = CH1−CH4), `supply: dict` (`ref_sag_mv`, `ref_p1`, `gnd_dev_mv`, `gnd_p99`, `window`), `compare: dict | None`, `correlations: list[dict]`, `findings: list[Finding]`, `.codes: set[str]`, `.events: list[(t, kind)]`. `SignalReport` has `n_edges, usable, gaps, missing, extra, runt_hi, runt_lo, revs, revs_good, rpm_median, bad_revs, events, anomalies, edges, label`. `Finding(code, severity in {"info","warn","fail"}, title, detail, evidence)`. Finding codes: `signal_clean, no_signal, harness_mismatch, ref_sag, ground_lift, unexplained_dropouts, no_sync_gap, insufficient_data`.

- [ ] **Step 1: Write the failing test**

`tests/test_analysis.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_analysis.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crank.analysis'`

- [ ] **Step 3: Write the implementation**

`crank/analysis.py`:

```python
"""Pure analysis of crank captures: edges, gaps, runts, supply correlation, verdict.

No scope, disk, or UI access. Everything takes arrays and a Thresholds object.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .model import CaptureData, Thresholds


@dataclass
class SignalReport:
    name: str
    label: str
    n_edges: int = 0
    edges: np.ndarray = field(default_factory=lambda: np.empty(0))
    usable: bool = False
    gaps: int = 0
    missing: int = 0
    extra: int = 0
    runt_hi: int = 0
    runt_lo: int = 0
    high_median: float | None = None
    high_lowest: float | None = None
    revs: int = 0
    revs_good: int = 0
    rpm_median: float | None = None
    rpm_min: float | None = None
    rpm_max: float | None = None
    bad_revs: list = field(default_factory=list)      # (time, edge_count)
    events: list = field(default_factory=list)        # (time, kind)

    @property
    def anomalies(self) -> int:
        return self.missing + self.extra + self.runt_hi + self.runt_lo + len(self.bad_revs)


@dataclass
class Finding:
    code: str
    severity: str          # "info" | "warn" | "fail"
    title: str
    detail: str
    evidence: dict = field(default_factory=dict)


@dataclass
class Analysis:
    reports: dict
    supply: dict
    compare: dict | None
    correlations: list
    findings: list

    @property
    def codes(self) -> set:
        return {f.code for f in self.findings}

    @property
    def events(self) -> list:
        return sorted(e for r in self.reports.values() for e in r.events)


def schmitt(v: np.ndarray, lo: float, hi: float) -> np.ndarray:
    above, below = v > hi, v < lo
    idx = np.flatnonzero(above | below)
    if idx.size == 0:
        return np.zeros(v.size, bool)
    st = above[idx]
    pos = np.searchsorted(idx, np.arange(v.size), side="right") - 1
    return np.where(pos >= 0, st[np.clip(pos, 0, None)], st[0])


def analyze_signal(name: str, label: str, t: np.ndarray, v: np.ndarray,
                   th: Thresholds) -> SignalReport:
    s = schmitt(v, th.lo, th.hi)
    rise = np.flatnonzero(~s[:-1] & s[1:]) + 1
    rep = SignalReport(name=name, label=label, n_edges=len(rise), edges=t[rise])
    if len(rise) < th.min_edges:
        return rep
    rep.usable = True
    tr = t[rise]
    per = np.diff(tr)
    ratio = np.ones_like(per)
    for i in range(len(per)):
        lo_i = max(0, i - 4)
        nb = np.delete(per[lo_i:i + 5], i - lo_i)
        ratio[i] = per[i] / np.median(nb)
    ratio[:th.skip_first_edges] = 1.0
    gaps = np.flatnonzero(ratio > 2.5)
    drops = np.flatnonzero((ratio > 1.6) & (ratio <= 2.5))
    extra = np.flatnonzero(ratio < 0.6)

    # runt / weak pulses: cross the midpoint but never reach the full levels
    mid = (th.hi + th.lo) / 2
    m = v > mid
    starts = np.concatenate(([0], np.flatnonzero(m[1:] != m[:-1]) + 1))
    vmax = np.maximum.reduceat(v, starts)
    vmin = np.minimum.reduceat(v, starts)
    state = m[starts]
    inner = lambda ix: ix[(ix > 0) & (ix < len(starts) - 1)]
    runt_hi = inner(np.flatnonzero(state & (vmax < th.runt_hi)))
    runt_lo = inner(np.flatnonzero(~state & (vmin > th.runt_lo)))

    rep.gaps, rep.missing, rep.extra = len(gaps), len(drops), len(extra)
    rep.runt_hi, rep.runt_lo = len(runt_hi), len(runt_lo)
    highs = vmax[state]
    if highs.size:
        rep.high_median, rep.high_lowest = float(np.median(highs)), float(highs.min())

    if len(gaps) >= 2:
        counts = np.diff(gaps)
        revt = tr[gaps[1:] + 1] - tr[gaps[:-1] + 1]
        rpm = 60.0 / revt
        bad = np.flatnonzero(counts != th.teeth)
        rep.revs, rep.revs_good = len(counts), len(counts) - len(bad)
        rep.rpm_median, rep.rpm_min, rep.rpm_max = (
            float(np.median(rpm)), float(rpm.min()), float(rpm.max()))
        rep.bad_revs = [(float(tr[gaps[j] + 1]), int(counts[j])) for j in bad]

    ev = [(float(tr[i + 1]), "missing edge?") for i in drops]
    ev += [(float(tr[i + 1]), "extra edge") for i in extra]
    ev += [(float(t[starts[i]]), "runt high") for i in runt_hi]
    ev += [(float(t[starts[i]]), "weak low") for i in runt_lo]
    rep.events = sorted(ev)
    return rep


def supply_stats(t: np.ndarray, chans: dict, th: Thresholds, window) -> dict:
    mask = (t >= window[0]) & (t <= window[1]) if window else np.ones(t.size, bool)
    out: dict = {"window": window}
    if 3 in chans:
        v = chans[3][mask]
        p1 = float(np.percentile(v, 1))
        out.update(ref_p1=p1, ref_median=float(np.median(v)),
                   ref_sag_mv=max(0.0, th.ref_nominal - p1) * 1000)
    if 4 in chans:
        v = chans[4][mask]
        p99 = float(np.percentile(np.abs(v), 99))
        out.update(gnd_p99=p99, gnd_dev_mv=p99 * 1000)
    return out


def compare_edges(a: SignalReport, b: SignalReport) -> dict | None:
    if a.n_edges < 2 or b.n_edges < 2:
        return None
    a1, a2 = a.edges, b.edges
    j = np.clip(np.searchsorted(a1, a2), 1, len(a1) - 1)
    near = np.where(np.abs(a1[j] - a2) < np.abs(a1[j - 1] - a2), a1[j], a1[j - 1])
    dly = near - a2
    return dict(n1=a.n_edges, n2=b.n_edges,
                delay_median_us=float(np.median(dly) * 1e6),
                delay_worst_us=float(np.abs(dly).max() * 1e6))


def correlate_events(t, chans, events, th: Thresholds, limit=500) -> list:
    if not events or (3 not in chans and 4 not in chans):
        return []
    dt = float(np.median(np.diff(t[:1000])))
    w = max(1, int(th.event_window_s / dt))
    out = []
    for te, kind in events[:limit]:
        i = int(np.searchsorted(t, te))
        row = dict(t=te, kind=kind, ref_dev_mv=0.0, gnd_dev_mv=0.0)
        if 3 in chans:
            seg = chans[3][max(0, i - w):i + w + 1] - th.ref_nominal
            row["ref_dev_mv"] = float(seg[np.argmax(np.abs(seg))] * 1000)
        if 4 in chans:
            seg = chans[4][max(0, i - w):i + w + 1]
            row["gnd_dev_mv"] = float(seg[np.argmax(np.abs(seg))] * 1000)
        row["aligned"] = bool(abs(row["ref_dev_mv"]) > 0.5 * th.ref_sag_mv
                              or abs(row["gnd_dev_mv"]) > 0.5 * th.ground_mv)
        out.append(row)
    return out


def build_findings(reports, supply, cmp, corr, th: Thresholds) -> list:
    F: list[Finding] = []

    def add(code, sev, title, detail, **ev):
        F.append(Finding(code, sev, title, detail, ev))

    r1, r2 = reports.get("ch1"), reports.get("ch2")
    live = [r for r in (r1, r2) if r is not None]
    if not live:
        add("insufficient_data", "warn", "No crank channels in this capture",
            "Enable CH1 (crank at the PCM pin) and/or CH2 (crank at the sensor connector).")
        return F

    usable = [r for r in live if r.usable]
    ref_sag = supply.get("ref_sag_mv", 0.0) >= th.ref_sag_mv
    gnd_lift = supply.get("gnd_dev_mv", 0.0) >= th.ground_mv
    anomalous = any(r.usable and r.anomalies >= max(3, 0.01 * r.n_edges)
                    for r in reports.values())
    aligned = [c for c in corr if c["aligned"]]
    align_txt = (f" {len(aligned)} of {len(corr)} dropout events coincide with a supply/ground "
                 f"disturbance." if corr else "")

    if not usable:
        names = " and ".join(r.label for r in live)
        add("no_signal", "fail", "No usable crank signal",
            f"{names}: fewer than {th.min_edges} clean rising edges. With the sensor wiring "
            f"verified, check the sensor's 5V reference, its ground, and whether the "
            f"reluctor is turning.", edges={r.name: r.n_edges for r in live})
    elif len(live) == 2 and r1.usable != r2.usable:
        dead = r1 if not r1.usable else r2
        add("harness_mismatch", "fail",
            f"{dead.label} has no usable signal but the other crank channel does",
            "The same signal looks different at the two probe points: suspect the wiring "
            "or connector between them (or a probe problem on the dead channel).",
            edges={r.name: r.n_edges for r in live})
    elif cmp and abs(cmp["n1"] - cmp["n2"]) > 0.05 * max(cmp["n1"], cmp["n2"]):
        add("harness_mismatch", "fail",
            "Crank edge counts differ between PCM pin and sensor connector",
            f"{cmp['n1']} vs {cmp['n2']} rising edges: suspect the harness or connector.",
            **cmp)

    if ref_sag:
        add("ref_sag", "fail" if anomalous or not usable else "warn",
            f"5V reference sags {supply['ref_sag_mv']:.0f} mV during cranking",
            f"Lowest 5V ref (1st percentile) is {supply['ref_p1']:.2f} V." + align_txt,
            ref_sag_mv=supply["ref_sag_mv"])
    if gnd_lift:
        add("ground_lift", "fail" if anomalous or not usable else "warn",
            f"PCM low reference deviates {supply['gnd_dev_mv']:.0f} mV from battery negative",
            "The PCM's ground lifts relative to the battery during cranking, so the signal it "
            "sees (CH1 - CH4) is shifted." + align_txt, gnd_dev_mv=supply["gnd_dev_mv"])
    if usable and anomalous and not (ref_sag or gnd_lift):
        worst = max(usable, key=lambda r: r.anomalies)
        add("unexplained_dropouts", "fail",
            "Dropouts or runt pulses with a steady supply and ground",
            f"{worst.label}: {worst.missing} missing-edge suspects, {worst.runt_hi} runt highs, "
            f"{worst.runt_lo} weak lows, {len(worst.bad_revs)} revolutions with the wrong edge "
            f"count. 5V ref and PCM ground were steady, so suspect the sensor, its air gap, "
            f"or the reluctor wheel.", anomalies=worst.anomalies)
    if usable and not any(r.gaps >= 2 for r in usable):
        add("no_sync_gap", "warn", "No 58x sync gap found",
            "Fewer than 2 gaps were found, so edges per revolution cannot be counted. Check "
            "the teeth setting, that this is the crank (not cam) signal, and the reluctor wheel.")

    if not any(f.severity in ("warn", "fail") for f in F):
        add("signal_clean", "info", "Crank signal looks clean at the scope",
            "Edges, gaps, levels, 5V reference and PCM ground all look good in this capture. "
            "If the PCM still shows 0 RPM, look at the PCM side: power, ground, calibration.")
    return F


def analyze_capture(data: CaptureData, th: Thresholds | None = None) -> Analysis:
    th = th or Thresholds()
    t, ch = data.t, data.channels
    reports: dict[str, SignalReport] = {}
    for n in (1, 2):
        if n in ch:
            reports[f"ch{n}"] = analyze_signal(f"ch{n}", data.labels.get(n, f"CH{n}"), t, ch[n], th)
    if 1 in ch and 4 in ch:
        reports["ch1_rel"] = analyze_signal("ch1_rel", "Crank as PCM sees it (CH1-CH4)",
                                            t, ch[1] - ch[4], th)
    live = [reports[k].edges for k in ("ch1", "ch2") if k in reports and reports[k].usable]
    window = (min(e[0] for e in live), max(e[-1] for e in live)) if live else None
    supply = supply_stats(t, ch, th, window)
    cmp = (compare_edges(reports["ch1"], reports["ch2"])
           if "ch1" in reports and "ch2" in reports else None)
    events = sorted(e for r in reports.values() for e in r.events)
    corr = correlate_events(t, ch, events, th)
    return Analysis(reports, supply, cmp, corr, build_findings(reports, supply, cmp, corr, th))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_analysis.py -v`
Expected: 39 passed (27 scenario/seed combinations plus 12 targeted tests)

- [ ] **Step 5: Commit**

```bash
git add crank/analysis.py tests/test_analysis.py
git commit -m "feat: crank signal analysis and verdict" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Scope layer (RigolScope, SimScope, self-check)

**Files:**
- Create: `crank/scope.py`
- Test: `tests/test_scope.py`

**Interfaces:**
- Consumes: `CaptureData`, `CaptureSettings` (Task 1); `generate` (Task 2, imported lazily inside `SimScope.fetch`).
- Produces: `ScopeError`; abstract `ScopeInterface` with `.log: list[str]`, `identify()`, `configure(settings)`, `arm()`, `status() -> "WAIT"|"RUN"|"AUTO"|"TD"|"STOP"`, `fetch(settings) -> CaptureData`, `disarm()`, `close()`. `RigolScope(resource=None, inst=None)` adds `self_check(settings) -> list[(ok: bool, text: str)]`. `SimScope(scenario="healthy", arm_polls=3, fault=None, seed=1)` where `fault` is `None | "never_arms" | "stuck_waiting" | "fetch_error"`.

- [ ] **Step 1: Write the failing test**

`tests/test_scope.py` (`FakeVisa` is reused by `tests/test_app.py` in Task 8):

```python
import numpy as np
import pytest

from crank.model import CaptureSettings
from crank.scope import RigolScope, ScopeError, SimScope


class FakeVisa:
    """Replays canned SCPI answers; optionally rejects commands."""
    def __init__(self, reject=()):
        self.writes, self.reject, self._errs, self.state = [], reject, [], {}
        self.timeout = self.chunk_size = None

    def write(self, cmd):
        self.writes.append(cmd)
        name, _, value = cmd.partition(" ")
        self.state[name] = value
        if any(cmd.startswith(r) for r in self.reject):
            self._errs.append('-113,"Undefined header"')

    def query(self, cmd):
        if cmd == ":SYSTem:ERRor:NEXT?":
            return self._errs.pop(0) if self._errs else '0,"No error"'
        if cmd.endswith("?") and cmd[:-1] in self.state:
            return self.state[cmd[:-1]]
        return {"*IDN?": "RIGOL,DHO804", ":ACQuire:MDEPth?": "10M",
                ":ACQuire:SRATe?": "1.6E6", ":TRIGger:STATus?": "STOP",
                ":WAVeform:PREamble?": "0,0,300000,1,1e-5,-0.3,0,0.02,0,127"}[cmd]

    def query_binary_values(self, cmd, datatype, container):
        return np.full(250_000, 200, dtype=np.uint8)

    def close(self):
        pass


def test_configure_sends_critical_commands_in_order():
    f = FakeVisa()
    RigolScope(inst=f).configure(CaptureSettings())
    order = [c for c in f.writes if c.startswith((":STOP", ":TIMebase:MAIN:SCALe", ":ACQuire:MDEPth",
                                                  ":TRIGger:SWEep"))]
    assert [c.split()[0] for c in order] == [":STOP", ":TIMebase:MAIN:SCALe", ":ACQuire:MDEPth",
                                             ":TRIGger:SWEep"]
    assert ":ACQuire:TYPE NORMal" in f.writes and ":ACQuire:MDEPth 10M" in f.writes


def test_critical_rejection_raises_noncritical_logs():
    with pytest.raises(ScopeError, match="MDEPth"):
        RigolScope(inst=FakeVisa(reject=(":ACQuire:MDEPth",))).configure(CaptureSettings())
    s = RigolScope(inst=FakeVisa(reject=(":CHANnel1:BWLimit",)))
    s.configure(CaptureSettings())
    assert any("BWLimit" in m for m in s.log)


def test_fetch_converts_to_volts_and_time():
    s = RigolScope(inst=FakeVisa())
    cfg = CaptureSettings()
    cfg.channels = cfg.channels[:1]
    d = s.fetch(cfg, chunk=100_000)
    assert len(d.t) == 300_000 and np.isclose(d.t[0], -0.3)
    assert np.isclose(d.channels[1][0], (200 - 0 - 127) * 0.02)


def test_short_record_logs_warning():
    f = FakeVisa()
    f.query_orig = f.query
    f.query = lambda cmd: ("0,0,5000,1,1e-5,-0.3,0,0.02,0,127" if cmd == ":WAVeform:PREamble?"
                           else f.query_orig(cmd))
    f.query_binary_values = lambda cmd, datatype, container: np.full(5000, 100, dtype=np.uint8)
    s = RigolScope(inst=f)
    cfg = CaptureSettings()
    cfg.channels = cfg.channels[:1]
    s.fetch(cfg)
    assert any("only 5000 points" in m for m in s.log)


def test_sim_scope_flow_and_channel_filter():
    sc = SimScope("healthy", arm_polls=2)
    cfg = CaptureSettings()
    cfg.channels[1].enabled = False
    sc.configure(cfg)
    sc.arm()
    assert [sc.status() for _ in range(5)] == ["WAIT", "WAIT", "TD", "STOP", "STOP"]
    assert set(sc.fetch(cfg).channels) == {1, 3, 4}


def test_self_check_passes_then_flags_a_clamped_setting():
    results = RigolScope(inst=FakeVisa()).self_check(CaptureSettings())
    assert results and all(ok for ok, _ in results)

    f = FakeVisa()
    sc = RigolScope(inst=f)
    real_query = f.query
    f.query = lambda c: "1M" if c == ":ACQuire:MDEPth?" else real_query(c)   # scope clamped the depth
    bad = [t for ok, t in sc.self_check(CaptureSettings()) if not ok]
    assert len(bad) == 1 and "memory depth" in bad[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_scope.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crank.scope'`

- [ ] **Step 3: Write the implementation**

`crank/scope.py`:

```python
"""Scope interface, the real Rigol DHO800 implementation, and the simulator."""
from __future__ import annotations

import sys
from abc import ABC, abstractmethod

import numpy as np

from .model import CaptureData, CaptureSettings


class ScopeError(Exception):
    """A scope problem the user needs to see (connection, rejected critical setting)."""


class ScopeInterface(ABC):
    def __init__(self):
        self.log: list[str] = []          # SCPI rejections and warnings, shown in the UI

    @abstractmethod
    def identify(self) -> str: ...

    @abstractmethod
    def configure(self, s: CaptureSettings) -> None: ...

    @abstractmethod
    def arm(self) -> None: ...

    @abstractmethod
    def status(self) -> str:
        """One of WAIT, RUN, AUTO, TD (triggered), STOP."""

    @abstractmethod
    def fetch(self, s: CaptureSettings) -> CaptureData: ...

    def disarm(self) -> None:
        pass

    def close(self) -> None:
        pass


# ----------------------------------------------------------------- real scope
# Commands whose rejection means the capture cannot be trusted.
CRITICAL = (":ACQuire:MDEPth", ":TIMebase", ":TRIGger", ":WAVeform:MODE", ":SINGle")


def _depth_value(text: str) -> float:
    """'10M' and '1.0E+7' both mean ten million points."""
    t = text.strip().upper()
    mult = {"K": 1e3, "M": 1e6}.get(t[-1:], 1.0)
    return float(t[:-1] if t[-1:] in ("K", "M") else t) * mult


class RigolScope(ScopeInterface):
    def __init__(self, resource: str | None = None, inst=None):
        """`inst` lets tests inject a fake VISA instrument."""
        super().__init__()
        if inst is None:
            inst = self._open(resource)
        self.i = inst
        self.i.timeout = 30000
        self.i.chunk_size = 1 << 20

    @staticmethod
    def _open(resource):
        try:
            import pyvisa
            rm = pyvisa.ResourceManager("@py")
            if not resource:
                found = [r for r in rm.list_resources() if r.startswith(("USB", "TCPIP"))]
                rig = [r for r in found if "0X1AB1" in r.upper()] or found
                if not rig:
                    raise ScopeError("No scope found. Check the USB cable, or enter a resource "
                                     "such as TCPIP0::192.168.1.50::INSTR.")
                resource = rig[0]
            return rm.open_resource(resource)
        except ScopeError:
            raise
        except Exception as e:
            raise ScopeError(f"Could not open the scope ({e}). If another program (for example "
                             f"the Rigol tool in Claude Code) has it open, close that first.") from e

    # -- low level
    def q(self, cmd: str) -> str:
        return self.i.query(cmd).strip()

    def drain_errors(self) -> list[str]:
        errs = []
        for _ in range(8):
            e = self.q(":SYSTem:ERRor:NEXT?")
            if e.startswith("0,") or e == "0":
                break
            errs.append(e)
        return errs

    def w(self, cmd: str) -> None:
        self.i.write(cmd)
        errs = self.drain_errors()
        if not errs:
            return
        msg = f"'{cmd}' rejected: {'; '.join(errs)}"
        if cmd.startswith(CRITICAL):
            raise ScopeError(msg)
        self.log.append(msg)

    # -- interface
    def identify(self) -> str:
        return self.q("*IDN?")

    def configure(self, s: CaptureSettings) -> None:
        scale = s.seconds / 10.0                       # 10 horizontal divisions
        self.w(":STOP")
        self.w(":TIMebase:MODE MAIN")
        self.w(f":TIMebase:MAIN:SCALe {scale}")
        # trigger point sits `pretrig` of the screen from the left (verify on screen)
        self.w(f":TIMebase:MAIN:OFFSet {(0.5 - s.pretrig) * 10 * scale}")
        self.w(":ACQuire:TYPE NORMal")
        self.w(f":ACQuire:MDEPth {s.depth}")
        got = self.q(":ACQuire:MDEPth?")
        self.log.append(f"Memory depth requested {s.depth}, scope reports {got}; "
                        f"sample rate {self.q(':ACQuire:SRATe?')}")
        for c in s.channels:
            self.w(f":CHANnel{c.n}:DISPlay {'ON' if c.enabled else 'OFF'}")
            if not c.enabled:
                continue
            self.w(f":CHANnel{c.n}:COUPling DC")
            self.w(f":CHANnel{c.n}:PROBe 1")
            self.w(f":CHANnel{c.n}:BWLimit OFF")
            self.w(f":CHANnel{c.n}:SCALe {c.scale}")
            self.w(f":CHANnel{c.n}:OFFSet {c.offset}")
        self.w(":TRIGger:MODE EDGE")
        self.w(":TRIGger:EDGE:SOURce CHANnel1")
        self.w(":TRIGger:EDGE:SLOPe POSitive")
        self.w(f":TRIGger:EDGE:LEVel {s.trig_level}")
        self.w(":TRIGger:SWEep SINGle")

    def self_check(self, s: CaptureSettings) -> list[tuple[bool, str]]:
        """Configure, then read every setting back and report mismatches."""
        self.configure(s)
        scale = s.seconds / 10.0
        numeric = [(":TIMebase:MAIN:SCALe?", scale, "timebase scale (s/div)"),
                   (":TIMebase:MAIN:OFFSet?", (0.5 - s.pretrig) * 10 * scale, "horizontal offset (s)"),
                   (":TRIGger:EDGE:LEVel?", s.trig_level, "trigger level (V)")]
        for c in s.channels:
            if c.enabled:
                numeric += [(f":CHANnel{c.n}:SCALe?", c.scale, f"CH{c.n} scale (V/div)"),
                            (f":CHANnel{c.n}:OFFSet?", c.offset, f"CH{c.n} offset (V)")]
        text = [(":TRIGger:EDGE:SOURce?", "CHANnel1", "trigger source"),
                (":TRIGger:EDGE:SLOPe?", "POSitive", "trigger slope"),
                (":TRIGger:SWEep?", "SINGle", "trigger sweep")]
        out = []
        for cmd, want, name in numeric:
            try:
                got = float(self.q(cmd))
                out.append((abs(got - want) <= 1e-3 * max(abs(want), 1e-6),
                            f"{name}: asked {want:g}, scope reports {got:g}"))
            except Exception as e:
                out.append((False, f"{name}: query failed ({e})"))
        try:
            got = self.q(":ACQuire:MDEPth?")
            out.append((_depth_value(got) == _depth_value(s.depth),
                        f"memory depth: asked {s.depth}, scope reports {got}"))
        except Exception as e:
            out.append((False, f"memory depth: query failed ({e})"))
        for cmd, want, name in text:
            try:
                got = self.q(cmd)
                out.append((got[:4].upper() == want[:4].upper(),
                            f"{name}: asked {want}, scope reports {got}"))
            except Exception as e:
                out.append((False, f"{name}: query failed ({e})"))
        return out

    def arm(self) -> None:
        self.w(":SINGle")

    def status(self) -> str:
        return self.q(":TRIGger:STATus?")

    def disarm(self) -> None:
        self.i.write(":STOP")

    def fetch(self, s: CaptureSettings, chunk: int = 250_000) -> CaptureData:
        channels, t = {}, None
        for c in s.channels:
            if not c.enabled:
                continue
            t, channels[c.n] = self._fetch_channel(c.n, chunk)
        if t is None:
            raise ScopeError("No channels enabled.")
        labels = {c.n: c.label for c in s.channels if c.enabled}
        return CaptureData(t, channels, labels, {"sim": False, "idn": self.identify()})

    def _fetch_channel(self, ch: int, chunk: int):
        self.w(f":WAVeform:SOURce CHANnel{ch}")
        self.w(":WAVeform:MODE RAW")
        self.w(":WAVeform:FORMat BYTE")
        pre = [float(x) for x in self.q(":WAVeform:PREamble?").split(",")]
        n, xinc, xorg, xref, yinc, yorg, yref = (
            int(pre[2]), pre[4], pre[5], pre[6], pre[7], pre[8], pre[9])
        if n < 100_000:
            self.log.append(f"CH{ch}: preamble reports only {n} points; the scope is likely not "
                            f"returning full RAW memory (check memory depth / WAVeform:MODE RAW).")
        raw = np.empty(n, dtype=np.uint8)
        for start in range(1, n + 1, chunk):
            stop = min(start + chunk - 1, n)
            self.w(f":WAVeform:STARt {start}")
            self.w(f":WAVeform:STOP {stop}")
            d = self.i.query_binary_values(":WAVeform:DATA?", datatype="B", container=np.array)
            raw[start - 1:stop] = d[: stop - start + 1]
        v = (raw.astype(np.float32) - yorg - yref) * yinc
        t = (np.arange(n) - xref) * xinc + xorg
        return t, v

    def close(self) -> None:
        try:
            self.i.close()
        except Exception:
            pass


# ----------------------------------------------------------------- simulator
class SimScope(ScopeInterface):
    """Plays back a generated scenario. `fault` injects capture-flow failures:
    None, "never_arms" (status stays STOP), "stuck_waiting" (never triggers),
    or "fetch_error" (download raises)."""

    def __init__(self, scenario: str = "healthy", arm_polls: int = 3,
                 fault: str | None = None, seed: int = 1):
        super().__init__()
        self.scenario, self.arm_polls, self.fault, self.seed = scenario, arm_polls, fault, seed
        self._polls = 0
        self._armed = False

    def identify(self) -> str:
        return f"SIMULATOR,{self.scenario}"

    def configure(self, s: CaptureSettings) -> None:
        self.log.append(f"Simulator configured for scenario '{self.scenario}'.")

    def arm(self) -> None:
        self._polls, self._armed = 0, self.fault != "never_arms"

    def status(self) -> str:
        if not self._armed:
            return "STOP"
        if self.fault == "stuck_waiting":
            return "WAIT"
        self._polls += 1
        if self._polls <= self.arm_polls:
            return "WAIT"
        if self._polls == self.arm_polls + 1:
            return "TD"
        return "STOP"

    def disarm(self) -> None:
        self._armed = False

    def fetch(self, s: CaptureSettings) -> CaptureData:
        from .sim import generate
        if self.fault == "fetch_error":
            raise ScopeError("Simulated download failure.")
        d = generate(self.scenario, seconds=s.seconds, pretrig=s.pretrig, seed=self.seed)
        keep = {c.n for c in s.channels if c.enabled}
        d.channels = {n: v for n, v in d.channels.items() if n in keep}
        d.labels = {c.n: c.label for c in s.channels if c.enabled}
        return d
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_scope.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add crank/scope.py tests/test_scope.py
git commit -m "feat: scope interface, Rigol implementation with self-check, simulator" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Capture job

**Files:**
- Create: `crank/capture.py`
- Test: `tests/test_capture.py`

**Interfaces:**
- Consumes: `ScopeInterface`, `ScopeError` (Task 4); `analyze_capture`, `Analysis` (Task 3); `CaptureSettings`, `Thresholds`, `CaptureData` (Task 1).
- Produces: `CaptureJob(scope, settings, thresholds=None, save=None, poll_s=0.2, never_armed_s=3.0)` with `.start()`, `.cancel()`, `.join(timeout)`, `.running`, `.seconds_left`, `.state` (one of `idle, configuring, armed, triggered, downloading, analyzing, done, error, cancelled`), `.message`, `.data`, `.analysis`, `.saved_id` (whatever `save(data)` returned). Constants `STATES`, `ACTIVE`.

- [ ] **Step 1: Write the failing test**

`tests/test_capture.py`:

```python
import time

import pytest

from crank.capture import CaptureJob
from crank.model import CaptureSettings
from crank.scope import SimScope


def _job(scope, **kw):
    s = CaptureSettings(wait=kw.pop("wait", 5))
    j = CaptureJob(scope, s, poll_s=0.0, never_armed_s=0.05, **kw)
    j.start()
    j.join(5)
    return j


def test_job_happy_path_saves_before_analysis(tmp_path):
    saved = []
    j = _job(SimScope("healthy"), save=saved.append)
    assert j.state == "done" and saved and j.analysis.codes == {"signal_clean"}


@pytest.mark.parametrize("fault,expect", [("never_arms", "never armed"),
                                          ("fetch_error", "download failure")])
def test_job_faults_become_error_state(fault, expect):
    j = _job(SimScope("healthy", fault=fault))
    assert j.state == "error" and expect in j.message


def test_job_timeout():
    j = _job(SimScope("healthy", fault="stuck_waiting"), wait=0.2)
    assert j.state == "error" and "Timed out" in j.message


def test_job_cancel_disarms():
    sc = SimScope("healthy", fault="stuck_waiting")
    j = CaptureJob(sc, CaptureSettings(wait=30), poll_s=0.01)
    j.start()
    time.sleep(0.1)
    j.cancel()
    j.join(5)
    assert j.state == "cancelled" and sc._armed is False


def test_analysis_crash_keeps_saved_data():
    saved = []
    j = CaptureJob(SimScope("healthy"), CaptureSettings(), poll_s=0.0, save=saved.append,
                   thresholds=object())          # breaks analysis on purpose
    j.start(); j.join(5)
    assert j.state == "error" and saved
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_capture.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crank.capture'`

- [ ] **Step 3: Write the implementation**

`crank/capture.py`:

```python
"""CaptureJob: configure, arm, wait for trigger, download, save, analyze, in a thread."""
from __future__ import annotations

import threading
import time
from typing import Callable

from .analysis import Analysis, analyze_capture
from .model import CaptureData, CaptureSettings, Thresholds
from .scope import ScopeError, ScopeInterface

STATES = ("idle", "configuring", "armed", "triggered", "downloading",
          "analyzing", "done", "error", "cancelled")
ACTIVE = ("configuring", "armed", "triggered", "downloading", "analyzing")


class CaptureJob:
    def __init__(self, scope: ScopeInterface, settings: CaptureSettings,
                 thresholds: Thresholds | None = None,
                 save: Callable[[CaptureData], object] | None = None,
                 poll_s: float = 0.2, never_armed_s: float = 3.0):
        self.scope, self.settings = scope, settings
        self.thresholds = thresholds or Thresholds()
        self.save, self.poll_s, self.never_armed_s = save, poll_s, never_armed_s
        self.state, self.message = "idle", ""
        self.data: CaptureData | None = None
        self.analysis: Analysis | None = None
        self.saved_id = None
        self.started_at = 0.0
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    # -- control
    def start(self) -> None:
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self.state in ACTIVE

    @property
    def seconds_left(self) -> float:
        return max(0.0, self.settings.wait - (time.time() - self.started_at))

    # -- worker
    def _set(self, state: str, message: str = "") -> None:
        self.state, self.message = state, message

    def _run(self) -> None:
        try:
            self._set("configuring")
            self.scope.configure(self.settings)
            self.scope.arm()
            self._set("armed", "Crank the engine now.")
            if not self._wait_for_trigger():
                return
            self._set("downloading")
            self.data = self.scope.fetch(self.settings)
            if self.save:
                self.saved_id = self.save(self.data)  # on disk before analysis can fail
            self._set("analyzing")
            self.analysis = analyze_capture(self.data, self.thresholds)
            self._set("done")
        except ScopeError as e:
            self._set("error", str(e))
        except Exception as e:                        # analysis or storage bug: data is kept
            self._set("error", f"{type(e).__name__}: {e}")

    def _wait_for_trigger(self) -> bool:
        t0, seen_armed = time.time(), False
        while True:
            if self._cancel.is_set():
                self.scope.disarm()
                self._set("cancelled", "Capture cancelled; scope disarmed.")
                return False
            st = self.scope.status()
            if st != "STOP":
                seen_armed = True
                if st == "TD":
                    self._set("triggered", "Trigger seen; recording.")
            elif seen_armed:
                return True
            elif time.time() - t0 > self.never_armed_s:
                self._set("error", "The scope never armed after SINGLE (status stayed STOP). "
                                   "Check the SCPI log for rejected commands.")
                return False
            if time.time() - t0 > self.settings.wait:
                self.scope.disarm()
                self._set("error", "Timed out waiting for a trigger. Is CH1 crossing the "
                                   "trigger level while cranking?")
                return False
            time.sleep(self.poll_s)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_capture.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add crank/capture.py tests/test_capture.py
git commit -m "feat: CaptureJob state machine" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Session store

**Files:**
- Create: `crank/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `CaptureData` (Task 1).
- Produces: `SessionStore(root="sessions")` with `.list_sessions() -> list[str]`, `.open(name) -> Session` (creates; sanitizes the name; raises `ValueError` on an empty name). `Session` has `.name`, `.path`, `.add_capture(data, label="", note="") -> str` (ids `"001"`, `"002"`, ...), `.list_captures() -> list[dict]` (`id, label, note, time, scenario, sim`), `.load_capture(id)`, `.readings() -> dict`, `.set_reading(step_id, values)`, `.notes()`, `.set_notes(text)`.

- [ ] **Step 1: Write the failing test**

`tests/test_store.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crank.store'`

- [ ] **Step 3: Write the implementation**

`crank/store.py`:

```python
"""Sessions on disk: captures (.npz + sidecar JSON), manual readings, notes."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .model import CaptureData


def _safe(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip()).strip("._")
    if not s:
        raise ValueError("name must contain letters or digits")
    return s


def _write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        path.replace(path.with_suffix(path.suffix + ".corrupt"))   # keep it, start fresh
        return default


class Session:
    def __init__(self, path: Path):
        self.path = path
        self.name = path.name
        (path / "captures").mkdir(parents=True, exist_ok=True)

    # captures
    def _ids(self) -> list[str]:
        return sorted(p.stem for p in (self.path / "captures").glob("*.npz"))

    def add_capture(self, data: CaptureData, label: str = "", note: str = "") -> str:
        ids = self._ids()
        cid = f"{(int(ids[-1]) + 1) if ids else 1:03d}"
        data.save(str(self.path / "captures" / f"{cid}.npz"))
        _write_json(self.path / "captures" / f"{cid}.json", {
            "id": cid, "label": label or f"capture {cid}", "note": note,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "scenario": data.meta.get("scenario"), "sim": data.meta.get("sim", False)})
        return cid

    def list_captures(self) -> list[dict]:
        out = []
        for cid in self._ids():
            meta = _read_json(self.path / "captures" / f"{cid}.json", None)
            out.append(meta or {"id": cid, "label": f"capture {cid}", "note": "",
                                "time": "", "scenario": None, "sim": False})
        return out

    def load_capture(self, cid: str) -> CaptureData:
        return CaptureData.load(str(self.path / "captures" / f"{cid}.npz"))

    # manual readings from the guided procedure
    def readings(self) -> dict:
        return _read_json(self.path / "readings.json", {})

    def set_reading(self, step_id: str, values: dict) -> None:
        r = self.readings()
        r[step_id] = values
        _write_json(self.path / "readings.json", r)

    def notes(self) -> str:
        return _read_json(self.path / "notes.json", {}).get("text", "")

    def set_notes(self, text: str) -> None:
        _write_json(self.path / "notes.json", {"text": text})


class SessionStore:
    def __init__(self, root: str | Path = "sessions"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def list_sessions(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def open(self, name: str) -> Session:
        """Create the session if it does not exist."""
        return Session(self.root / _safe(name))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_store.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add crank/store.py tests/test_store.py
git commit -m "feat: file-based session store" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Guided procedure data and report/plot helpers

**Files:**
- Create: `crank/procedure.py`, `crank/report.py`
- Test: `tests/test_procedure.py`, `tests/test_report.py`

**Interfaces:**
- Consumes: `Analysis` (Task 3), `CaptureData` (Task 1), `generate` (Task 2, tests only).
- Produces: `STEPS: tuple[Step]`, `step_by_id(id)`, `Step(id, title, kind in {"manual","capture","checks"}, instructions, expected, fields)`, `Field(key, label, kind in {"number","bool","text"}, unit)`. `text_report(analysis) -> str`; `to_csv(data) -> bytes`; `decimate(t, v, max_points=20000)` (min/max envelope, keeps single-sample runts); `build_figure(data, analysis, x0=None, x1=None, max_events=300) -> plotly Figure` (one row per channel plus CH1−CH4; events drawn as one trace per row, because `add_vline` per event takes minutes); `build_overlay(a, b, ch, names, x0, x1)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_procedure.py`:

```python
from crank.procedure import STEPS, step_by_id


def test_step_ids_unique_and_fields_unique_per_step():
    assert len({s.id for s in STEPS}) == len(STEPS)
    for s in STEPS:
        assert len({f.key for f in s.fields}) == len(s.fields)
        assert s.kind in ("manual", "capture", "checks")


def test_step_by_id():
    assert step_by_id("static").title.startswith("1.")
```

`tests/test_report.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_procedure.py tests/test_report.py -v`
Expected: FAIL with `ModuleNotFoundError` for `crank.procedure` / `crank.report`

- [ ] **Step 3: Write the implementations**

`crank/procedure.py`:

```python
"""The guided test procedure as data. The UI renders it; nothing here touches Streamlit."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str = "number"      # "number" | "bool" | "text"
    unit: str = ""


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    kind: str                 # "manual" | "capture" | "checks"
    instructions: str
    expected: str
    fields: tuple = ()


STEPS = (
    Step("static", "1. Static baseline", "manual",
         "Key off. Measure battery voltage at the posts. With a DVOM on ohms, measure resistance "
         "battery negative to engine block, block to chassis, and battery negative to the PCM "
         "ground pin.",
         "Battery about 12.4-12.7 V at rest. Ground paths well under 1 ohm (yours measured "
         "0.3-0.5 ohm earlier). Compare against later readings; a change after a fix is meaningful.",
         (Field("battery_v", "Battery voltage", unit="V"),
          Field("gnd_batt_block_ohm", "Battery neg to block", unit="ohm"),
          Field("gnd_block_chassis_ohm", "Block to chassis", unit="ohm"),
          Field("gnd_batt_pcm_ohm", "Battery neg to PCM ground pin", unit="ohm"))),
    Step("koeo", "2. Key on, engine off", "manual",
         "Key on, engine off. With the DVOM on DC volts and the black lead on battery negative, "
         "measure the 5V reference at the crank sensor connector, the PCM low reference, and the "
         "crank signal wire at the PCM pin with the engine stationary.",
         "5V reference about 4.9-5.1 V. Low reference near 0 V (tens of millivolts). Signal wire "
         "sitting at a steady high or low level.",
         (Field("ref5v_v", "5V ref at sensor connector", unit="V"),
          Field("low_ref_v", "PCM low ref vs battery neg", unit="V"),
          Field("signal_static_v", "Crank signal at PCM pin, stationary", unit="V"))),
    Step("baseline_capture", "3. Baseline cranking capture", "capture",
         "Wire the scope: CH1 crank at the PCM pin, CH2 crank at the sensor connector, CH3 5V "
         "reference, CH4 PCM low reference. All probe grounds go to battery negative. Arm on the "
         "Capture page, then crank for several seconds. Optionally log the lowest battery voltage "
         "you see on the DVOM while cranking.",
         "A battery that stays above about 9.6 V while cranking is a common rule of thumb. The "
         "verdict on the Results page tells you what the capture shows.",
         (Field("battery_crank_min_v", "Lowest battery voltage while cranking", unit="V"),)),
    Step("after_change", "4. Capture after each change", "capture",
         "After each change (re-ground, sensor swap, connector cleaning), capture again and put "
         "what you changed in the capture label. Use the Compare page to see whether it helped.",
         "Edges per revolution, runt counts, and supply/ground deviation should move toward the "
         "healthy values after a real fix."),
    Step("mechanical", "5. Balancer and reluctor check", "checks",
         "Check the crank sensor for a tight mount and correct air gap, the reluctor wheel for "
         "damage or debris, and that the harmonic balancer has not slipped on the crank.",
         "Everything tight, clean and aligned. A slipped balancer usually causes wrong timing "
         "rather than a 0 RPM reading, so it is a low-probability cause here.",
         (Field("sensor_mount_ok", "Sensor tight, air gap OK", kind="bool"),
          Field("reluctor_ok", "Reluctor wheel clean and undamaged", kind="bool"),
          Field("balancer_ok", "Balancer marks aligned, no slip", kind="bool"),
          Field("notes", "Notes", kind="text"))),
)


def step_by_id(step_id: str) -> Step:
    for s in STEPS:
        if s.id == step_id:
            return s
    raise KeyError(step_id)
```

`crank/report.py`:

```python
"""Text report and Plotly figures built from a CaptureData + Analysis."""
from __future__ import annotations

import io

import numpy as np

from .analysis import Analysis
from .model import CaptureData

SEVERITY_ORDER = {"fail": 0, "warn": 1, "info": 2}


def text_report(a: Analysis) -> str:
    out = []
    for f in sorted(a.findings, key=lambda f: SEVERITY_ORDER[f.severity]):
        out.append(f"[{f.severity.upper()}] {f.title}\n    {f.detail}")
    out.append("")
    for r in a.reports.values():
        out.append(f"{r.label}: {r.n_edges} rising edges" + (
            f", {r.revs_good}/{r.revs} good revolutions, median {r.rpm_median:.0f} RPM, "
            f"{r.missing} missing-edge suspects, {r.runt_hi} runt highs, {r.runt_lo} weak lows"
            if r.usable and r.rpm_median is not None else
            " (no usable signal)" if not r.usable else ""))
    s = a.supply
    if "ref_p1" in s:
        out.append(f"5V ref: 1st percentile {s['ref_p1']:.2f} V (sag {s['ref_sag_mv']:.0f} mV)")
    if "gnd_p99" in s:
        out.append(f"PCM low ref vs battery negative: 99th percentile |V| {s['gnd_p99']:.3f} V")
    if a.compare:
        c = a.compare
        out.append(f"PCM pin vs sensor connector: {c['n1']} vs {c['n2']} edges, "
                   f"delay median {c['delay_median_us']:.1f} us, worst {c['delay_worst_us']:.1f} us")
    return "\n".join(out)


def to_csv(data: CaptureData) -> bytes:
    cols = sorted(data.channels)
    arr = np.column_stack([data.t] + [data.channels[n] for n in cols])
    buf = io.StringIO()
    np.savetxt(buf, arr, delimiter=",", fmt="%.6g",
               header=",".join(["t_s"] + [data.labels.get(n, f"ch{n}") for n in cols]), comments="")
    return buf.getvalue().encode()


def decimate(t: np.ndarray, v: np.ndarray, max_points: int = 20000):
    """Min/max envelope decimation so runts and glitches survive downsampling."""
    n = len(v)
    if n <= max_points:
        return t, v
    bins = max_points // 2
    size = n // bins
    vv, tt = v[:bins * size].reshape(bins, size), t[:bins * size].reshape(bins, size)
    imin, imax = np.nanargmin(vv, axis=1), np.nanargmax(vv, axis=1)
    rows = np.arange(bins)
    i0, i1 = np.minimum(imin, imax), np.maximum(imin, imax)
    return (np.stack([tt[rows, i0], tt[rows, i1]], axis=1).ravel(),
            np.stack([vv[rows, i0], vv[rows, i1]], axis=1).ravel())


def build_figure(data: CaptureData, a: Analysis, x0: float | None = None,
                 x1: float | None = None, max_events: int = 300):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    t = data.t
    x0 = t[0] if x0 is None else x0
    x1 = t[-1] if x1 is None else x1
    m = (t >= x0) & (t <= x1)
    rows = [(data.labels.get(n, f"CH{n}"), data.channels[n]) for n in sorted(data.channels)]
    if 1 in data.channels and 4 in data.channels:
        rows.append(("Crank as PCM sees it (CH1-CH4)", data.channels[1] - data.channels[4]))
    fig = make_subplots(rows=len(rows), cols=1, shared_xaxes=True, vertical_spacing=0.03,
                        subplot_titles=[r[0] for r in rows])
    ev = [e[0] for e in a.events if x0 <= e[0] <= x1][:max_events]
    for k, (name, v) in enumerate(rows, start=1):
        td, vd = decimate(t[m], v[m])
        fig.add_trace(go.Scatter(x=td, y=vd, mode="lines", name=name, line=dict(width=1),
                                 showlegend=False), row=k, col=1)
        if ev and len(vd):
            lo, hi = float(np.nanmin(vd)), float(np.nanmax(vd))
            xs = np.repeat(ev, 3).astype(float)
            xs[2::3] = np.nan
            ys = np.tile([lo, hi, np.nan], len(ev))
            fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", hoverinfo="skip", showlegend=False,
                                     line=dict(color="rgba(220,0,0,0.35)", width=1)), row=k, col=1)
    fig.update_xaxes(title_text="s from trigger", row=len(rows), col=1)
    fig.update_layout(height=220 * len(rows) + 60, margin=dict(l=40, r=10, t=40, b=40))
    return fig


def build_overlay(a: CaptureData, b: CaptureData, ch: int, names=("A", "B"),
                  x0: float | None = None, x1: float | None = None):
    import plotly.graph_objects as go
    fig = go.Figure()
    for d, nm in ((a, names[0]), (b, names[1])):
        if ch not in d.channels:
            continue
        lo = d.t[0] if x0 is None else x0
        hi = d.t[-1] if x1 is None else x1
        m = (d.t >= lo) & (d.t <= hi)
        td, vd = decimate(d.t[m], d.channels[ch][m])
        fig.add_trace(go.Scatter(x=td, y=vd, mode="lines", name=nm, line=dict(width=1)))
    fig.update_layout(height=320, margin=dict(l=40, r=10, t=20, b=40), xaxis_title="s from trigger")
    return fig
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_procedure.py tests/test_report.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add crank/procedure.py crank/report.py tests/test_procedure.py tests/test_report.py
git commit -m "feat: guided procedure data, text report, plot helpers" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Streamlit app

**Files:**
- Create: `app.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: everything from Tasks 1-7. `tests/test_app.py` imports `FakeVisa` from `tests/test_scope.py`.
- Produces: `streamlit run app.py`. Environment variables `CRANK_SESSIONS_DIR` (default `sessions`) and `CRANK_POLL_S` (default `0.2`, tests set `0`). Stable widget keys used by tests: `mode`, `scenario`, `connect`, `self_check`, `page`, `arm`, `cancel`, `view_results`, `f_<step>_<field>`, `save_<step>`.

- [ ] **Step 1: Write the failing test**

`tests/test_app.py`:

```python
import os

import pytest
from streamlit.testing.v1 import AppTest

APP = os.path.join(os.path.dirname(__file__), "..", "app.py")


@pytest.fixture
def at(tmp_path, monkeypatch):
    monkeypatch.setenv("CRANK_SESSIONS_DIR", str(tmp_path))
    monkeypatch.setenv("CRANK_POLL_S", "0")
    app = AppTest.from_file(APP, default_timeout=60)
    app.run()
    assert not app.exception
    return app


def _arm_and_finish(at):
    at.button(key="arm").click().run()
    for _ in range(10):
        if at.session_state["job"].state in ("done", "error"):
            break
        at.run()
    return at.session_state["job"]


def test_app_loads_with_simulator_default(at):
    assert at.sidebar.radio(key="mode").value == "Simulator"
    assert at.session_state["page"] == "Capture"


def test_arm_simulator_completes_and_saves(at):
    job = _arm_and_finish(at)
    assert job.state == "done" and job.analysis.codes == {"signal_clean"}
    assert at.session_state["store"].open("default").list_captures()


def test_results_page_shows_verdict_for_faulty_scenario(at):
    at.sidebar.selectbox(key="scenario").select("ref_sag").run()
    _arm_and_finish(at)
    at.sidebar.radio(key="page").set_value("Results").run()
    assert not at.exception
    assert any("5V reference sags" in m.value for m in at.markdown)


def test_results_page_empty_session_is_friendly(at):
    at.sidebar.radio(key="page").set_value("Results").run()
    assert not at.exception and any("No captures" in i.value for i in at.info)


def test_guided_step_saves_reading(at):
    at.sidebar.radio(key="page").set_value("Guided tests").run()
    at.number_input(key="f_static_battery_v").set_value(12.6).run()
    at.button(key="save_static").click().run()
    assert at.session_state["store"].open("default").readings()["static"]["battery_v"] == 12.6


def test_real_scope_without_connection_shows_error(at):
    at.sidebar.radio(key="mode").set_value("Real scope").run()
    at.button(key="arm").click().run()
    assert any("Connect to the scope first" in e.value for e in at.error)


def test_compare_two_captures(at):
    _arm_and_finish(at)
    at.sidebar.selectbox(key="scenario").select("runt_pulses").run()
    _arm_and_finish(at)
    at.sidebar.radio(key="page").set_value("Compare").run()
    assert not at.exception


def test_self_check_button_reports_each_setting(at):
    from tests.test_scope import FakeVisa
    from crank.scope import RigolScope
    at.sidebar.radio(key="mode").set_value("Real scope").run()
    at.session_state["scope"] = RigolScope(inst=FakeVisa())
    at.run()
    at.sidebar.button(key="self_check").click().run()
    assert not at.exception
    assert any("memory depth" in s.value for s in at.sidebar.success)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_app.py -v`
Expected: FAIL (`FileNotFoundError` or an `AppTest` error because `app.py` does not exist)

- [ ] **Step 3: Write the implementation**

`app.py`:

```python
"""Crank signal test harness UI. Run with:  streamlit run app.py"""
import dataclasses
import os
import time

import streamlit as st

from crank.analysis import analyze_capture
from crank.capture import CaptureJob
from crank.model import CaptureSettings, ChannelConfig, Thresholds
from crank.procedure import STEPS
from crank.report import build_figure, build_overlay, to_csv
from crank.scope import RigolScope, ScopeError, SimScope
from crank.sim import SCENARIOS
from crank.store import SessionStore

ROOT = os.environ.get("CRANK_SESSIONS_DIR", "sessions")
POLL_S = float(os.environ.get("CRANK_POLL_S", "0.2"))
PAGES = ["Capture", "Results", "Guided tests", "Compare"]
SEVERITY_ICON = {"fail": "🔴", "warn": "🟠", "info": "🟢"}

st.set_page_config(page_title="Crank Test Harness", layout="wide")
ss = st.session_state
ss.setdefault("store", SessionStore(ROOT))
ss.setdefault("session_name", "default")
ss.setdefault("settings", CaptureSettings())
ss.setdefault("thresholds", Thresholds())
ss.setdefault("job", None)
ss.setdefault("scope", None)
ss.setdefault("view", None)           # capture id shown on Results
ss.setdefault("page", "Capture")


def goto(page, view=None):
    ss.page = page
    if view:
        ss.view = view


def session():
    return ss.store.open(ss.session_name)


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.title("Crank Test Harness")
    mode = st.radio("Scope", ["Simulator", "Real scope"], key="mode")
    if mode == "Simulator":
        st.selectbox("Scenario", SCENARIOS, key="scenario")
    else:
        st.text_input("VISA resource (blank = auto-detect)", key="resource")
        if st.button("Connect", key="connect"):
            try:
                if ss.scope:
                    ss.scope.close()
                ss.scope = RigolScope(ss.get("resource") or None)
                st.success(f"Connected: {ss.scope.identify()}")
            except ScopeError as e:
                ss.scope = None
                st.error(str(e))
        if ss.scope:
            st.caption("Connected to a real scope.")
            if st.button("Self-check settings", key="self_check"):
                try:
                    for ok, line in ss.scope.self_check(ss.settings):
                        (st.success if ok else st.error)(line)
                except ScopeError as e:
                    st.error(str(e))
                if ss.scope.log:
                    st.code("\n".join(ss.scope.log))

    st.divider()
    names = ss.store.list_sessions()
    if ss.session_name not in names:
        session()                                     # make sure the active session exists
        names = ss.store.list_sessions()
    st.selectbox("Session", names, index=names.index(ss.session_name), key="session_pick")
    ss.session_name = ss.session_pick
    new = st.text_input("New session name", key="new_session")
    if st.button("Create session", key="create_session") and new.strip():
        try:
            ss.session_name = ss.store.open(new).name
            st.rerun()
        except ValueError as e:
            st.error(str(e))
    st.divider()
    st.radio("Page", PAGES, key="page")


# ------------------------------------------------------------------ capture
def page_capture():
    st.header("Capture")
    s: CaptureSettings = ss.settings
    st.info("Wiring: CH1 crank at the PCM pin, CH2 crank at the sensor connector, CH3 5V reference, "
            "CH4 PCM low reference. All probe grounds to battery negative.")
    job: CaptureJob | None = ss.job
    busy = bool(job and job.running)

    with st.expander("Channels and settings", expanded=not busy):
        rows = st.data_editor(
            [dataclasses.asdict(c) for c in s.channels], key="chan_editor", hide_index=True,
            disabled=busy, column_config={"n": st.column_config.NumberColumn("CH", disabled=True)})
        c1, c2, c3, c4 = st.columns(4)
        seconds = c1.number_input("Capture length (s)", 1.0, 30.0, s.seconds, disabled=busy)
        pretrig = c2.number_input("Pre-trigger (fraction)", 0.0, 0.9, s.pretrig, disabled=busy)
        depth = c3.selectbox("Memory depth", ["1M", "10M", "25M"],
                             index=["1M", "10M", "25M"].index(s.depth), disabled=busy)
        trig = c4.number_input("Trigger level (V)", -10.0, 10.0, s.trig_level, disabled=busy)
        ss.settings = CaptureSettings(seconds, pretrig, depth, trig, s.wait,
                                      [ChannelConfig(**r) for r in rows])

    label = st.text_input("Capture label (what changed?)", key="cap_label")
    note = st.text_input("Note", key="cap_note")

    if st.button("Arm", key="arm", type="primary", disabled=busy):
        if mode == "Simulator":
            scope = SimScope(ss.scenario)
        elif ss.scope:
            scope = ss.scope
        else:
            st.error("Connect to the scope first.")
            return
        sess = session()
        ss.job = CaptureJob(scope, ss.settings, ss.thresholds, poll_s=POLL_S,
                            save=lambda d: sess.add_capture(d, label, note))
        ss.job.start()
        st.rerun()

    job = ss.job
    if job is None:
        return
    if job.running:
        if job.state in ("armed", "triggered"):
            st.warning(f"**ARMED: crank the engine now.**  Waiting {job.seconds_left:.0f} s more. "
                       f"State: {job.state}")
        else:
            st.info(f"State: {job.state}")
        if st.button("Cancel", key="cancel"):
            job.cancel()
        time.sleep(0.5)
        st.rerun()
    elif job.state == "done":
        st.success("Capture complete and saved.")
        st.button("View results", key="view_results", on_click=goto, args=("Results", job.saved_id))
    elif job.state in ("error", "cancelled"):
        (st.error if job.state == "error" else st.warning)(job.message)
    log = getattr(job.scope, "log", [])
    if log:
        with st.expander("Scope log (SCPI messages)", expanded=job.state == "error"):
            st.code("\n".join(log))


# ------------------------------------------------------------------ results
def show_verdict(a):
    order = {"fail": 0, "warn": 1, "info": 2}
    for f in sorted(a.findings, key=lambda f: order[f.severity]):
        with st.container(border=True):
            st.markdown(f"{SEVERITY_ICON[f.severity]} **{f.title}**")
            st.write(f.detail)


def summary_row(a):
    r = a.reports.get("ch1") or next(iter(a.reports.values()), None)
    s = a.supply
    return {
        "edges (CH1)": r.n_edges if r else None,
        "good revs": f"{r.revs_good}/{r.revs}" if r and r.usable else "n/a",
        "median RPM": round(r.rpm_median) if r and r.rpm_median else None,
        "runt highs": r.runt_hi if r else None,
        "missing-edge suspects": r.missing if r else None,
        "5V ref sag (mV)": round(s.get("ref_sag_mv", 0)),
        "ground dev (mV)": round(s.get("gnd_dev_mv", 0)),
    }


def thresholds_editor():
    with st.expander("Analysis thresholds"):
        vals = {}
        cols = st.columns(3)
        for i, f in enumerate(dataclasses.fields(Thresholds)):
            cur = getattr(ss.thresholds, f.name)
            kind = int if isinstance(cur, int) else float
            vals[f.name] = kind(cols[i % 3].number_input(f.name, value=cur, key=f"th_{f.name}"))
        ss.thresholds = Thresholds(**vals)


def page_results():
    st.header("Results")
    sess = session()
    caps = sess.list_captures()
    if not caps:
        st.info("No captures in this session yet. Use the Capture page.")
        return
    ids = [c["id"] for c in caps]
    default = ss.view if ss.view in ids else ids[-1]
    cid = st.selectbox("Capture", ids, index=ids.index(default), key="result_pick",
                       format_func=lambda i: f"{i}: {next(c['label'] for c in caps if c['id'] == i)}")
    thresholds_editor()
    data = sess.load_capture(cid)
    a = analyze_capture(data, ss.thresholds)
    show_verdict(a)
    st.dataframe([summary_row(a)], hide_index=True)

    t0, t1 = float(data.t[0]), float(data.t[-1])
    x0, x1 = st.slider("Time window (s)", t0, t1, (t0, t1), key="window")
    st.plotly_chart(build_figure(data, a, x0, x1), use_container_width=True)
    if a.events:
        with st.expander(f"Events ({len(a.events)})"):
            st.dataframe([{"t (s)": round(t, 5), "event": k} for t, k in a.events], hide_index=True)
    c1, c2 = st.columns(2)
    c1.download_button("Download .npz", open(sess.path / "captures" / f"{cid}.npz", "rb").read(),
                       file_name=f"{sess.name}_{cid}.npz")
    c2.download_button("Download CSV", to_csv(data), file_name=f"{sess.name}_{cid}.csv")


# ------------------------------------------------------------------ guided tests
def page_guided():
    st.header("Guided tests")
    sess = session()
    saved = sess.readings()
    for step in STEPS:
        done = step.id in saved
        with st.expander(f"{'✅ ' if done else ''}{step.title}", expanded=not done):
            st.markdown(f"**Do:** {step.instructions}")
            st.markdown(f"**Expect:** {step.expected}")
            vals = {}
            prev = saved.get(step.id, {})
            for f in step.fields:
                key = f"f_{step.id}_{f.key}"
                lab = f"{f.label} ({f.unit})" if f.unit else f.label
                if f.kind == "bool":
                    vals[f.key] = st.checkbox(lab, value=bool(prev.get(f.key, False)), key=key)
                elif f.kind == "text":
                    vals[f.key] = st.text_area(lab, value=prev.get(f.key, ""), key=key)
                else:
                    vals[f.key] = st.number_input(lab, value=float(prev.get(f.key, 0.0)),
                                                  format="%.3f", key=key)
            if step.fields and st.button("Save step", key=f"save_{step.id}"):
                sess.set_reading(step.id, vals)
                st.rerun()
            if step.kind == "capture":
                st.button("Go to Capture", key=f"go_{step.id}", on_click=goto, args=("Capture",))
    notes = st.text_area("Session notes", value=sess.notes(), key="session_notes")
    if st.button("Save notes", key="save_notes"):
        sess.set_notes(notes)
        st.success("Saved.")


# ------------------------------------------------------------------ compare
def page_compare():
    st.header("Compare")
    cols = st.columns(2)
    picks = []
    for side, col in zip("AB", cols):
        names = ss.store.list_sessions()
        sname = col.selectbox(f"Session {side}", names,
                              index=names.index(ss.session_name), key=f"cmp_sess_{side}")
        sess = ss.store.open(sname)
        caps = sess.list_captures()
        if not caps:
            col.info("No captures.")
            return
        cid = col.selectbox(f"Capture {side}", [c["id"] for c in caps], key=f"cmp_cap_{side}",
                            format_func=lambda i, caps=caps: f"{i}: {next(c['label'] for c in caps if c['id'] == i)}")
        data = sess.load_capture(cid)
        a = analyze_capture(data, ss.thresholds)
        picks.append((f"{side}: {sname}/{cid}", data, a))
        with col:
            show_verdict(a)
    st.dataframe([{"capture": n, **summary_row(a)} for n, _, a in picks], hide_index=True)
    chans = sorted(set(picks[0][1].channels) & set(picks[1][1].channels))
    if chans:
        ch = st.selectbox("Channel to overlay", chans, key="cmp_ch",
                          format_func=lambda n: picks[0][1].labels.get(n, f"CH{n}"))
        t0 = max(picks[0][1].t[0], picks[1][1].t[0])
        t1 = min(picks[0][1].t[-1], picks[1][1].t[-1])
        x0, x1 = st.slider("Time window (s)", float(t0), float(t1), (float(t0), float(t1)),
                           key="cmp_window")
        st.plotly_chart(build_overlay(picks[0][1], picks[1][1], ch, (picks[0][0], picks[1][0]), x0, x1),
                        use_container_width=True)


{"Capture": page_capture, "Results": page_results,
 "Guided tests": page_guided, "Compare": page_compare}[ss.page]()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_app.py -v`
Expected: 8 passed (about 15 seconds)

- [ ] **Step 5: Smoke-test the real server**

Run: `.venv/bin/streamlit run app.py --server.headless true --server.port 8765 &` then `curl -s localhost:8765/_stcore/health` (expected `ok`), then stop it with `kill %1`. Then open it for real: `.venv/bin/streamlit run app.py`, leave the sidebar on Simulator, pick `ref_sag`, click Arm, and confirm the Results page shows a red "5V reference sags" card and a five-row plot with red event markers.

- [ ] **Step 6: Commit**

```bash
git add app.py tests/test_app.py
git commit -m "feat: Streamlit UI (capture, results, guided tests, compare)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 9: CLI wrapper, README, final verification

**Files:**
- Modify: `crank_capture.py` (replace the whole file)
- Create: `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `CaptureJob`, `SimScope`, `RigolScope`, `analyze_capture`, `text_report`, `CaptureData`, `Thresholds`.
- Produces: `main(argv=None) -> int` with subcommands `capture [--sim SCENARIO] [--resource R] [--seconds S] [--depth D] [--out F] [--teeth N]` and `analyze FILE [--teeth N]`.

- [ ] **Step 1: Write the failing test**

`tests/test_cli.py`:

```python
import crank_capture


def test_cli_simulated_capture_then_analyze(tmp_path, capsys):
    out = str(tmp_path / "c.npz")
    assert crank_capture.main(["capture", "--sim", "ground_lift", "--out", out, "--poll", "0"]) == 0
    assert "PCM low reference deviates" in capsys.readouterr().out
    assert crank_capture.main(["analyze", out]) == 0
    assert "[FAIL]" in capsys.readouterr().out


def test_cli_missing_scope_fails_cleanly(capsys, monkeypatch):
    def boom(resource=None):
        from crank.scope import ScopeError
        raise ScopeError("No scope found.")
    monkeypatch.setattr(crank_capture, "RigolScope", boom)
    assert crank_capture.main(["capture"]) == 1
    assert "No scope found" in capsys.readouterr().err
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_cli.py -v`
Expected: FAIL (the old `crank_capture.py` has no `main(argv)` taking those subcommands; or it errors at import)

- [ ] **Step 3: Replace the script**

`crank_capture.py`:

```python
#!/usr/bin/env python3
"""Command-line front end to the crank harness (the Streamlit app is app.py).

  python crank_capture.py capture --sim ref_sag          # simulated scope
  python crank_capture.py capture --seconds 6            # real Rigol scope; arm, then crank
  python crank_capture.py analyze crank_capture.npz      # re-run analysis on a saved capture
"""
import argparse
import sys
import time

from crank.analysis import analyze_capture
from crank.capture import ACTIVE, CaptureJob
from crank.model import CaptureData, CaptureSettings, Thresholds
from crank.report import text_report
from crank.scope import RigolScope, ScopeError, SimScope
from crank.sim import SCENARIOS


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--sim", choices=SCENARIOS, help="use the simulator instead of a real scope")
    c.add_argument("--resource", help="VISA resource string (default: auto-detect)")
    c.add_argument("--seconds", type=float, default=6.0)
    c.add_argument("--depth", default="10M")
    c.add_argument("--out", default="crank_capture.npz")
    c.add_argument("--poll", type=float, default=0.2, help=argparse.SUPPRESS)
    a = sub.add_parser("analyze")
    a.add_argument("file")
    for sp in (c, a):
        sp.add_argument("--teeth", type=int, default=58, help="rising edges per crank revolution")
    args = p.parse_args(argv)
    th = Thresholds(teeth=args.teeth)

    if args.mode == "analyze":
        print(text_report(analyze_capture(CaptureData.load(args.file), th)))
        return 0

    try:
        scope = SimScope(args.sim) if args.sim else RigolScope(args.resource)
    except ScopeError as e:
        print(e, file=sys.stderr)
        return 1
    settings = CaptureSettings(seconds=args.seconds, depth=args.depth)
    job = CaptureJob(scope, settings, th, poll_s=args.poll,
                     save=lambda d: (d.save(args.out), args.out)[1])
    job.start()
    last = None
    while job.running or job.state == "idle":
        if job.state != last:
            last = job.state
            print(f"[{job.state}] {job.message}".rstrip())
        time.sleep(0.05)
    job.join()
    for line in scope.log:
        print("scope:", line)
    if job.state != "done":
        print(f"{job.state}: {job.message}", file=sys.stderr)
        return 1
    print(f"Saved {args.out}\n")
    print(text_report(job.analysis))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Write the README**

`README.md`:

````markdown
# Crank signal test harness

Diagnoses a 0-RPM-while-cranking symptom on a Gen IV LS (58x crank) by capturing the crank signal,
5V reference and PCM low reference on a Rigol DHO800 scope during cranking, then analyzing them.

## Setup

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt     # macOS: also `brew install libusb`

## Try it with no hardware

    .venv/bin/streamlit run app.py                 # leave the sidebar on "Simulator"
    .venv/bin/python crank_capture.py capture --sim ref_sag
    .venv/bin/python -m pytest

## First session at the truck

1. Wire the scope: CH1 crank at the PCM pin, CH2 crank at the sensor connector, CH3 5V reference,
   CH4 PCM low reference. All probe grounds go to battery negative.
2. Close any other program holding the scope's USB connection (for example the Rigol tool in Claude Code).
3. In the sidebar choose "Real scope", click Connect, then **Self-check settings**. Every line should be green.
   Red lines are SCPI settings the DHO804 clamped or rejected; the scope log shows the exact command.
4. Run the Guided tests, then arm on the Capture page and crank.
5. Check on the scope screen that the trigger point sits near the left edge of the capture.

## Not verified against real hardware yet

- The SCPI command strings (written from memory of the DHO800 programming guide).
- The horizontal-offset sign, valid memory depths in 4-channel mode, and the RAW preamble point count.
- 12-bit (WORD) waveform download is not implemented; data is 8-bit.

## Layout

    crank/model.py      shared dataclasses and the CaptureData container
    crank/sim.py        simulated captures for named fault scenarios
    crank/analysis.py   edge/gap/runt analysis and the verdict (pure functions)
    crank/scope.py      ScopeInterface, RigolScope (real), SimScope
    crank/capture.py    CaptureJob: arm, wait, download, save, analyze in a thread
    crank/store.py      sessions on disk
    crank/procedure.py  the guided test steps as data
    crank/report.py     text report and Plotly figures
    app.py              Streamlit UI
    crank_capture.py    command-line front end
````

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: 78 passed

- [ ] **Step 6: Manual end-to-end check (no hardware)**

```bash
.venv/bin/python crank_capture.py capture --sim ground_lift --out /tmp/x.npz
.venv/bin/python crank_capture.py analyze /tmp/x.npz
```

Expected: both print a `[FAIL] PCM low reference deviates ... mV` finding followed by per-channel summaries.

- [ ] **Step 7: Commit**

```bash
git add crank_capture.py README.md tests/test_cli.py
git commit -m "feat: CLI wrapper over the harness, README" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:** architecture and module boundaries (Tasks 1-8); eight simulator scenarios plus the `missing_teeth_no_gap` variant (Task 2); verdict rules and thresholds config (Task 3, `Thresholds`, editable in the Results page); CH1−CH4 derived trace (Task 3 analysis, Task 7 figure); `ScopeInterface`, `RigolScope`, `SimScope` (Task 4); `CaptureJob` states, cancel and timeout (Task 5); sessions, atomic writes, corrupt-file handling (Tasks 1 and 6); guided tests (Tasks 7-8); capture, results, guided, compare pages and sidebar (Task 8); scope self-check (Task 4 plus the sidebar button in Task 8); CLI wrapper (Task 9). Out of scope per the spec and not built: WORD format, live preview, multi-user, cloud sync.

**Placeholder scan:** none. Every code step shows the file in full.

**Type consistency:** `saved_id` is whatever `save(data)` returns; the app's save lambda returns the capture id string, the CLI's returns the output path. `Analysis.codes` is a `set[str]` and `EXPECTED_CODES` values are sets. Channel keys are ints everywhere (`CaptureData.channels`, `labels`), report keys are strings (`"ch1"`, `"ch2"`, `"ch1_rel"`).

**Known limits to carry forward:** SCPI behavior on the DHO804 is unverified (README lists the specifics); the verdict thresholds are starting values chosen against simulated data and will need tuning against the first real captures.
