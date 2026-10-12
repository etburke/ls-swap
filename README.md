# Crank signal test harness

A bench tool for diagnosing a **0 RPM while cranking** (or intermittent RPM) problem on a Gen IV LS
(58x crank) swap, here an LY5 5.3L with a PSI-reflashed E38 PCM in an '87 FJ60.

## What it's for

The PCM reads 0 RPM while the engine cranks, yet the crank signal looks fine on static checks and no
crank/cam correlation codes are set. The working theory is that something is **marginal under starter
load**: the crank signal, the sensor's 5V reference, or the PCM's low reference (ground). A static
multimeter check cannot see that, so this tool records all four signals on a Rigol DHO800 scope during
a real crank, then analyzes them and says where the problem most likely is.

| Scope channel | Signal (all probe grounds go to battery negative) |
|---|---|
| CH1 | Crank signal at the PCM pin |
| CH2 | Crank signal at the sensor connector |
| CH3 | 5V reference at the sensor connector |
| CH4 | PCM low reference vs battery negative |

## How it works

1. **Capture.** You arm the scope (single-shot, triggered on the first crank edge) and crank the engine.
   The tool downloads the full memory of all four channels and saves it to disk before doing anything else.
2. **Analyze.** It finds every rising edge, the 58x sync gap, and counts edges per revolution. It looks for
   missing edges, extra edges and runt pulses (pulses that never reach full level), and measures how far
   the 5V reference sags and the PCM ground lifts while cranking. It also analyzes CH1 minus CH4, which is
   the signal as the PCM actually sees it relative to its own ground.
3. **Verdict.** The findings are ranked and explained in plain language, with the evidence behind each one:

   | Finding | Meaning |
   |---|---|
   | No usable signal | Too few clean edges: check sensor power, ground, and whether the wheel turns |
   | Harness mismatch | CH1 (PCM pin) and CH2 (sensor connector) disagree: wiring or connector between them |
   | 5V reference sag | The reference collapses under starter load, with how many dropouts coincide with it |
   | Ground lift | The PCM's low reference moves away from battery negative while cranking |
   | Unexplained dropouts | Runts or missing edges with a steady supply and ground: sensor, air gap or reluctor wheel |
   | No sync gap | The 58x gap isn't found: wrong signal, wrong wheel setting, or a too-short capture |
   | Clean signal | Everything looks good at the scope, so look at the PCM side: power, ground, calibration |

   The verdict names a likely area and its evidence. It is not a diagnosis, and it says "insufficient data"
   rather than guess.
4. **Compare.** Every capture is stored in a session with a label ("after re-ground", "new sensor"), so you
   can overlay two captures and see whether a change helped.

A **simulator** with nine fault scenarios (healthy, no signal, ground lift, 5V sag, runt pulses, missing
teeth, missing gap, harness fault, late start) lets you run the whole tool, and its tests, with no hardware.

## Setup

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt     # macOS: also `brew install libusb`

## Using it

    .venv/bin/streamlit run app.py                 # then open http://localhost:8501

The sidebar picks **Simulator** or **Real scope**, and the session you're working in. Pages:

- **Case:** the vehicle profile, a quick-add form for symptoms, observations, codes, measurements, changes and
  suspects, a suspects board (open, ruled out, confirmed), the timeline, and the hand-off report.
- **Capture:** channel table and settings, an Arm button, a live "crank now" banner, Cancel, and a log of
  any SCPI commands the scope rejected.
- **Results:** the verdict, summary numbers, a zoomable plot of all channels with problem events marked, an
  event table, editable analysis thresholds, and `.npz` / CSV downloads.
- **Guided tests:** a step-by-step procedure (static baseline, key-on engine-off, baseline cranking capture,
  capture after each change, balancer and reluctor check) with fields for your multimeter readings,
  saved with the session.
- **Compare:** two captures side by side with their verdicts and an overlay of any channel.

Command line, same engine:

    .venv/bin/python crank_capture.py capture --sim ref_sag      # simulated scope
    .venv/bin/python crank_capture.py capture                    # real scope
    .venv/bin/python crank_capture.py analyze some_capture.npz   # re-analyze a saved file
    .venv/bin/python -m pytest                                   # test suite

### Cases and the hand-off report

Each session is a **case**: a vehicle profile plus an append-only event log (`events.jsonl`) of symptoms,
observations, codes, measurements, captures, changes and suspects. Captures and guided-test results are logged
automatically with the verdict as it was at the time; everything else you add on the Case page (or from the phone).
**Generate report** on the Case page produces Markdown (to paste into an email or forum post) and a
self-contained HTML page with the ruled-out list and before/after captures. The report contains only what was
recorded, shows gaps as "not recorded", labels simulator captures as simulated, and presents verdicts as what the
harness reported, not as a diagnosis. Sessions made before cases existed open as cases without any change to their files.

### Phone remote

`.streamlit/config.toml` makes the app listen on the network, and the laptop sidebar shows the phone URL,
for example `http://192.168.1.20:8501/?page=remote`. Open it on a phone on the same Wi-Fi to get a big ARM
button, Cancel, a live "crank now" banner, and the verdict when the capture finishes. It arms with whatever
scope mode, scenario, session and settings the laptop currently has; in Real-scope mode, click Connect on the
laptop first. There is no password: anyone on the network who finds the URL can arm the scope and browse
captures.

## First session at the truck

1. Wire the scope as in the table above. Close any other program holding the scope's USB connection.
2. In the sidebar choose **Real scope**, click **Connect**, then **Self-check settings**. Every line should be
   green; a red line is a setting the scope clamped or rejected, and the scope log shows the exact command.
3. Work through **Guided tests**, then arm on the Capture page (or the phone) and crank for several seconds.
4. Check on the scope screen that the trigger point sits near the left edge of the capture.

## Not verified against real hardware yet

- The SCPI command strings (written from memory of the DHO800 programming guide).
- The horizontal-offset sign, valid memory depths in 4-channel mode, and the RAW preamble point count.
- The verdict thresholds are tuned on simulated data and will need adjusting after the first real captures.
- 12-bit (WORD) waveform download is not implemented; data is 8-bit.

## Code layout

    crank/model.py       shared dataclasses and the CaptureData container
    crank/sim.py         simulated captures for named fault scenarios
    crank/analysis.py    edge/gap/runt analysis and the verdict (pure functions, no I/O)
    crank/scope.py       ScopeInterface, RigolScope (real, with self-check), SimScope
    crank/capture.py     CaptureJob: arm, wait, download, save, analyze in a background thread
    crank/controller.py  shared state so the phone and laptop drive the same capture
    crank/store.py       sessions on disk (captures, readings, notes)
    crank/case.py        a case: vehicle profile and the append-only event log
    crank/summary.py     structured, render-agnostic case summary
    crank/render.py      Markdown and self-contained HTML reports
    crank/procedure.py   the guided test steps as data
    crank/report.py      text report and Plotly figures
    app.py               Streamlit UI (laptop)
    case_ui.py           the Case page
    remote.py            phone remote page
    crank_capture.py     command-line front end
    tests/               pytest suite (no hardware needed)
