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

## Phone remote

`.streamlit/config.toml` makes the app listen on the network. The laptop sidebar shows the phone URL,
for example `http://192.168.1.20:8765/?page=remote`. Open it on a phone on the same Wi-Fi: big ARM and
Cancel buttons, a live "crank now" banner, and the verdict when the capture finishes. The phone arms
with whatever scope mode, scenario, session and settings the laptop currently has.
There is no password: anyone on the network who finds the URL can arm the scope and browse captures.
In Real-scope mode, click Connect on the laptop first.

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
