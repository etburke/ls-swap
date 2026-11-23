# Channel roles

## Intent

The analysis, plots and verdicts assume CH1 = crank at the PCM, CH2 = crank at the sensor, CH3 = 5V reference,
CH4 = PCM ground. Since the owner started moving probes (swapped CH2/CH3, then cam/EST/injector), every capture
has produced bogus verdicts ("harness mismatch", "5V reference sags 5068 mV", "ground lifts 10.6 V") and a
meaningless "Crank as PCM sees it (CH1-CH4)" plot. Each capture should record what every channel was connected
to, and the analysis and plots should follow that.

## Goals

- **Roles on the Capture page**: the channel table gets a Role column (dropdown) and, for per-cylinder or numbered
  circuits, a Number column (1-8). Defaults reproduce today's layout (crank at ECM, crank at sensor, crank 5V
  reference, crank low reference). Each capture saves its roles.
- **Analysis by role, basic level** (owner's pick): the existing checks run on the channels whose roles fit them:
  - crank checks (edges, gaps, revolutions, runts) on every crank-signal channel; the two-point harness comparison
    only when both a crank-at-ECM and a crank-at-sensor channel exist;
  - the 5V reference sag check on each 5V-reference channel (crank or cam);
  - the ground-lift check on each low-reference or ECM-ground channel;
  - "Crank as the ECM sees it" (crank at ECM minus crank low reference) only when both roles are present.
  Every other role is plotted and gets basic numbers only (minimum, median, maximum, pulse count, median pulse
  width); no verdicts.
- **Older captures**: no saved roles means the old fixed layout. Roles can be corrected on the Results page; the
  correction goes in the capture's `.json` file (the `.npz` scope data is not rewritten). Saving a correction
  re-analyses the capture and adds a new case entry ("re-analysed with corrected roles"); the original verdict
  entry stays.
- **MCP**: `get_capture_analysis` and `list_captures` report roles; new gated tool `set_capture_roles` (needs
  "Allow AI control") corrects a saved capture's roles and adds the same case entry.

## Roles (from the 2008 Vortec 5.3 E38 pinout, lt1swap.com)

Pins are shown as hints in the role picker (ECM side / sensor side).

| Role | E38 pin | Sensor pin | Analysis |
|---|---|---|---|
| Crank signal at ECM | J2-68 | | crank checks |
| Crank signal at sensor | | CKP A | crank checks |
| Crank 5V reference | J2-2 | CKP C | 5V sag |
| Crank low reference | J2-69 | CKP B | ground lift; ECM-relative crank |
| Cam signal | J2-64 | CMP C | basic |
| Cam 5V reference | J2-44 | CMP A | 5V sag |
| Cam low reference | J2-66 | CMP B | ground lift |
| Ignition control (EST), cyl n | J2-70/73/77/76/75/74/72/71 (IC 1-8) | coil | basic |
| Coil low reference, odd/even bank | J2-78 / J2-79 | coil E | ground lift |
| Injector control (low side), cyl n | J2-20/37/38/39/18/19/40/17 (inj 1-8) | | basic |
| ECM ground | J1-73 | | ground lift |
| ECM ignition 1 voltage | J1-19 / J1-47 | | basic |
| ECM battery voltage | J1-20 | | basic |
| Throttle position 1 / 2 | J2-65 / J2-63 | TB D / F | basic |
| Throttle actuator motor 1 / 2 | J2-6 / J2-5 | TB B / A | basic |
| Pedal position 1 / 2 | J1-29 / J1-32 | | basic |
| MAP signal | J2-58 | MAP B | basic |
| Fuel pump relay control | J1-50 | | basic |
| Starter enable relay control | J1-52 | | basic |
| Other | | | basic |

(The page does not tie IC n to a cylinder; the Number column is the circuit number the owner probed.)

## Constraints

- Scope data files are never rewritten; role corrections live in the capture `.json`.
- Case entries stay append-only.
- Captures saved before this change load and analyse exactly as today unless roles are corrected.

## Approaches considered

1. Roles stored with each capture, chosen in the channel table (**chosen**).
2. Roles guessed from labels: no new input, but a typo silently changes the analysis.
3. Roles picked only when viewing results: nothing saved; the case cannot say what was measured.

## Non-goals

- Cam pattern, EST dwell/firing angle, injector timing, sync and relative-compression analyses (the "full" level the
  owner declined for now).
- Changing the analysis thresholds.

## Builder's calls

How roles are encoded in files, the exact basic numbers' layout, plot ordering, UI wording, and test fixtures.
Builds on branch `feature/obd-pids-charts` (PR #23).
