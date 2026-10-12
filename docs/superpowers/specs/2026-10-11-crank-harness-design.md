# Crank Signal Test Harness — Design

Date: 2026-10-11

## Purpose

Diagnose a 0-RPM-while-cranking / intermittent-RPM symptom on an LY5 5.3L (Gen IV, 58x crank, 4X cam) with a PSI-reflashed E38 PCM in an '87 FJ60. Working theory: the crank signal, 5V reference, or PCM low reference is marginal under starter load. Clean signal at the PCM on static checks, no correlation codes (only P2119), a second E38 behaves identically, static ground resistance 0.3-0.5 ohm.

The owner is away from the truck and its Rigol DHO804 scope for 1+ week. Goal: a robust, user-friendly harness, built and tested without hardware, so the first session at home yields useful data quickly.

**Success:** at the truck, connect, arm, crank, and get a clear verdict with interactive plots, without editing code. Captures and manual readings are saved per session and comparable before/after each fix.

**Constraints**
- No scope access during development: all scope-facing logic is tested against a simulator. Real SCPI behavior is unverified until the first home session.
- Runs on a laptop at the truck, single user, Python.

## Scope

In: scope capture, analysis and verdict, simulator with fault scenarios, session storage, guided test procedure, capture comparison, Streamlit UI, CLI wrapper.

Out (for now): 12-bit WORD download format (add after first real capture), live scope preview, multi-user, cloud sync.

## Architecture

```
ls-swap/
  crank/
    scope.py        # ScopeInterface (abstract) + RigolScope (real SCPI)
    sim.py          # SimScope: synthetic captures from named fault scenarios
    capture.py      # CaptureJob: configure, arm, wait, download (background thread, status object)
    analysis.py     # pure functions: edges, gaps, runts, correlation, verdict (no I/O, no plotting)
    store.py        # sessions on disk: captures (.npz), manual readings, notes (JSON)
    procedure.py    # guided test steps as data
  app.py            # Streamlit UI only
  crank_capture.py  # CLI, thin wrapper over the modules
  tests/
```

Boundaries:
- `analysis.py` takes arrays and thresholds, returns plain data. No scope, disk, or UI access.
- `ScopeInterface`: `configure`, `arm`, `status`, `fetch`. `RigolScope` wraps the existing SCPI code; `SimScope` returns generated waveforms. The UI selects one via a Simulator / Real scope toggle.
- `CaptureJob` owns the thread and exposes state: idle, configuring, armed, triggered, downloading, analyzing, done, error.
- `store.py` keeps one folder per session with captures, DVOM readings, notes, and metadata.
- If Streamlit's rerun model proves awkward, NiceGUI is the fallback; the core modules do not change.

## Simulator scenarios

`SimScope` produces a ~6 s cranking capture: RPM ramps 0 to ~200 with compression ripple, 58x 0-5 V square wave with a 2-tooth gap and edge noise, plus 5V-ref and PCM-low-ref channels reflecting starter-current effects.

| Scenario | Injects |
|---|---|
| `healthy` | Clean signal, steady 5V ref, ground within ~50 mV |
| `no_signal` | CH1 flat (the case that crashed the original script) |
| `ground_lift` | PCM low ref rises 0.5-1.5 V during cranking |
| `ref_sag` | 5V ref drops toward 3.5-4 V when starter current hits |
| `runt_pulses` | Intermittent pulses reaching only 2.5-3.5 V |
| `missing_teeth` | Random dropped edges; variant with the gap deleted |
| `harness_fault` | CH2 (sensor connector) clean, CH1 (PCM pin) degraded |
| `late_start` | Cranking begins ~2 s after trigger (pre-trigger handling) |

Each scenario carries its expected verdict and doubles as a test fixture.

## Analysis and verdict

`analysis.py` returns a ranked list of findings: severity, plain-language explanation, supporting evidence. Rules:
- No edges on CH1: "no usable signal". CH2 clean implies harness or PCM pin; CH2 also dead implies sensor, its power, or its ground.
- CH1 and CH2 disagree (edge count or amplitude): harness or connector.
- Runts or missing edges time-aligned with 5V-ref sag or ground deviation: report which and by how many mV.
- Runts or missing edges with no supply/ground event: sensor or reluctor (air gap, wheel).
- All edges clean, 58 per revolution: signal fine at the scope; look at the PCM side (power, ground, calibration).
- Weak or inconsistent data: "insufficient data" with the reason.

The verdict reports likely area and evidence, not a diagnosis. Analysis also includes a derived CH1-CH4 trace (the signal as the PCM sees it relative to its own low reference). Thresholds live in one config object, editable in the UI.

## UI

Streamlit, sidebar plus four pages.

**Sidebar:** Simulator / Real scope toggle (default Simulator), resource string, Connect, scope ID; current session with New/Switch; scenario dropdown in simulator mode.

**Capture:** editable channel table (label, scale, offset, on/off; prefilled CH1 crank@PCM, CH2 crank@sensor, CH3 5V ref, CH4 PCM low ref vs battery negative; wiring reminder). Settings: capture length, pre-trigger, memory depth, trigger level. Arm button, live status, "Crank now" banner, countdown and Cancel (disarms). SCPI errors in a collapsible log. On completion, jumps to Results.

**Results:** verdict card with ranked findings and evidence; summary numbers (edges/rev, RPM, runt and missing counts, lowest high level, 5V-ref and ground deviation); Plotly view with linked zoom, event markers, jump-to-next-event, and the CH1-CH4 trace; download `.npz` and CSV.

**Guided tests:** fixed sequence, each step with instructions, probe placement, expected result, and an input for the reading: (1) static baseline (battery voltage, ground resistance per path); (2) key-on engine-off 5V ref and low ref by DVOM; (3) cranking capture with preset settings; (4) repeat captures after each change, labeled with what changed; (5) balancer and reluctor check (yes/no plus notes). Steps can be skipped or done out of order; entries save into the session.

**Compare:** pick two captures (any session), see summaries and verdicts side by side with overlaid traces.

Everything saves to the session folder as it happens.

## Testing

- **Analysis (pytest):** every sim scenario produces its expected verdict and numbers (edge counts, gap detection, runt counts); the empty-signal case is a permanent regression test.
- **Capture flow:** `CaptureJob` against `SimScope` through all states, including timeout, never-armed, and mid-download error.
- **Storage:** save/reload/compare round-trip; corrupt or partial files.
- **UI:** Streamlit `AppTest` headless against the simulator: arm, complete, view results, fill a guided step.
- **Real scope:** `RigolScope` gets a fake-VISA replay test verifying command order and parsing only; it cannot verify DHO804 acceptance. The first home session is the real test; the app includes a "scope self-check" button that configures and reads back settings so SCPI mismatches surface in minutes.

## Error handling

- **Scope:** every SCPI command followed by an error-queue check; rejections are logged to the UI with the offending command. Non-critical rejections warn and continue; critical ones (memory depth, trigger, RAW mode) stop the capture with a clear message.
- **Connection:** not found / busy produces a plain message ("close the other program using the scope") and a Retry.
- **Capture:** timeout with visible countdown and Cancel; short record, channel-count, or sample-rate mismatches warn before analysis.
- **Analysis:** weak data yields "insufficient data", never a guess or a crash.
- **Storage:** atomic writes (temp file then rename); the capture is saved before analysis runs.

## Known carry-over from the original script review

Already fixed in `crank_capture.py`: empty-edge crash, vendor-ID match, 10M default depth, `ACQuire:TYPE NORMal`, never-armed exit, short-record warning. To carry into `RigolScope`. Still open: SCPI details unverified on hardware (horizontal offset sign, valid memory depths in 4-channel mode, RAW preamble point count), and the optional WORD format.
