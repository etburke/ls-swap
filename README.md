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

## Channel roles

The table at the top is the classic wiring, and the default roles. Each capture records the role of every channel,
and the analysis follows the roles, not the channel numbers, so probes can go anywhere:

- **Crank signal at ECM** (E38 J2-68) and **at sensor** (CKP A): edge, gap, revolution and runt checks; with both,
  the two-point harness comparison.
- **5V references** (crank J2-2 / CKP C, cam J2-44 / CMP A): the sag check, on the worst one.
- **Low references and grounds** (crank J2-69, cam J2-66, coil odd/even J2-78/79, ECM ground J1-73): the
  ground-lift check, on the worst one. With crank at ECM and crank low reference both present, the analysis also
  checks the crank signal as the ECM sees it (the difference of the two).
- **Everything else** (cam signal J2-64, ignition control IC 1-8, injector low side 1-8, ECM ignition and battery
  feeds, throttle position and motor, pedal, MAP, fuel pump and starter relay control, other): plotted, with min /
  median / max, pulse count and median pulse width; no verdicts.

Pins are from a 2008 Vortec 5.3 E38 pinout (lt1swap.com); it does not say which cylinder each coil driver feeds, so
the number on an ignition-control channel is the circuit probed. Captures from before roles use the classic layout
until corrected.

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
  suspects, a suspects board (open, ruled out, confirmed), the timeline, and the hand-off report. It also has **Read from OBD**, which records codes, freeze frame and live values from the vehicle.
- **Capture:** channel table and settings, an Arm button, a live "crank now" banner, Cancel, and a log of
  any SCPI commands the scope rejected. Each channel has a **role**: what its probe is on (see Channel roles).
- **Results:** the verdict, summary numbers, a zoomable plot of all channels (titled by role) with problem events
  marked, numbers for channels no check applies to, an event table, editable analysis thresholds, and `.npz` / CSV
  downloads. **Channel roles** corrects what a saved capture's channels were connected to: saving re-analyses it
  and adds a "re-analysed with corrected roles" observation to the case (the original verdict stays). The
  correction is stored in the capture's `.json`; the scope data is never rewritten.
- **Watches:** reopen any saved OBD watch in the session and chart the PIDs you pick (engine RPM by default),
  one plot per unit on a shared time axis. For a watch linked to a capture, the RPM plot also shows the capture's
  per-revolution crank RPM from scope CH1 and marks the scope trigger.
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

### Reading the vehicle (OBD-II)

**Read from OBD** on the Case page (and **Read OBD** on the phone) asks the vehicle for its stored, pending and
permanent trouble codes, the freeze frame, and a one-shot snapshot of live values (RPM, battery voltage, coolant
temperature, fuel trims and a few more). Each read is added to the case: one `code_read` event plus a measurement for
every value the PCM answered. A value the PCM does not support is listed as not supported, never as zero, and a
real 0 RPM is kept as 0. The tool never clears codes.

The simulator has thirty-one scenarios (healthy, a stored P2119 with 0 RPM, many codes, a PCM with limited support, four
failure modes, and five multi-ECU ones: `pcm_and_trans`, `interleaved_multiframe`, `same_code_two_ecus`, `pcm_not_7e8`,
`trans_rejects_mode`, `slow_adapter` (every reply takes about a second, so you can try Stop and Cancel without hardware), `heartbeat_drop` (the adapter stops answering its heartbeat after three beats, so you can watch Dropped and Relink), and five for adapter trouble, below). **Real adapter** reads classic-Bluetooth ELM327/STN adapters (for example the OBDLink MX+) through
a serial port: pair the adapter in macOS Bluetooth settings first, then pick its port (or Auto-detect). Only
read-only requests are ever sent: modes 01 (one to six PIDs per request), 02, 03, 07 and 0A plus a fixed set of adapter setup commands. Anything
that could write adapter memory or put it to sleep (`ATPP`, `STSAVCAL`, `STSLEEP`, ...) is refused before it reaches
the port. Reads keep every ECU's answers apart: trouble codes from all ECUs are recorded and labeled with the ECU that
reported them (shown when more than one ECU answered), while live values, the freeze frame and the supported-PID map
come from the engine controller only (normally `7E8`). Only 11-bit CAN is supported. BLE adapters would plug in behind the same `Transport` interface
(`open`, `send`, `close`) via `bleak`.

The reader gives the first request 20 seconds for the adapter's protocol search (later requests get 5) and retries
a request once when the adapter reports a transient bus error such as `CAN ERROR` or `BUS BUSY`. If the adapter
resets itself (`LV RESET`, usually a supply dip), the reader re-applies its settings and retries once. Every retry
is listed in the read's notes, so a read that needed one says so; other adapter errors (`BUFFER FULL`, `STOPPED`,
and so on) fail the read with a specific message. A second read started while one is running is refused. The
simulator's `slow_search`, `transient_can_error`, `persistent_can_error`, `lv_reset_mid_read` and `buffer_full`
scenarios exercise these.

At the start of every read the reader asks the adapter who it is: `ATI` (the ELM327 version) and `STI`. If `STI`
answers with an STN firmware string such as `STN2255 v4.3.1`, it also asks `STDI` (device name) and `STSN` (serial
number). A plain ELM327 clone answers `?` and is recorded as "not an STN adapter, no ST features". The result is stored
in the case's `code_read` event, shown in the Read OBD result lines, and printed once in the case summary and report for
real (not simulated) reads. Identification never fails a read: a timeout or adapter error on an ID command is noted and
the read carries on. These four are the only identification commands on the allowlist; every other `ST*` command is
still refused. Nothing changes behaviour based on what was found yet; later work (`STPTO`, `STPX`) will use it. The
simulator's `elm_clone` and `flaky_adapter_id` scenarios exercise it. Not yet checked against a real adapter (#3).

**Listen-only monitor commands.** Six more adapter commands are allowed for the planned CAN monitor (OBD watch): `ATMA` (monitor all traffic), `ATCSM1` (the adapter stays silent on the bus, sending no ACKs), `ATCAF0` and `ATCAF1` (CAN automatic formatting off or on), and `ATD0` and `ATD1` (display of the DLC off or on). Monitor mode only listens; `ATCSM1` is what keeps the adapter from transmitting while it does so. Everything else stays refused: the transmit-capable commands (`ATCSM0`, which lets the adapter ACK on the bus, and `ATSH`, which sets a transmit header) and the other monitor variants that were not reviewed (`ATMT`, `ATMR`, `STMA`, ...). `ATMA` is only sent by the watch's stream, never as an ordinary command.

**When the PCM does not give a value.** A PID the PCM refuses, answers "busy" to, or does not answer never fails the read: the value is left out and the reason is recorded (not in the PCM's support map, refused by the PCM, PCM busy, no answer). A busy reply is retried once, under the same limit as adapter errors. The result lines group the missing values by reason, and the case summary and report print the latest read's notes (retries, resets, adapter gaps) and any values the PCM would not give. If a PID support map cannot be read, the PIDs it covers are tried anyway. Only a failure on the very first supported-PID request, or a busy reply on a trouble-code request, still fails the read. Trouble codes now show separate rows when a code is both stored and permanent. A case holds real OBD reads or simulated ones, never both: reading from the other kind is refused with a message, so practice runs cannot be mistaken for evidence. The simulator's `pid_refused`, `pid_busy_once`, `pid_busy_always`, `pid_no_answer`, `status_refused`, `map_refused`, `pcm_refuses_0100`, `trans_busy_on_pid` and `many_pids_busy` scenarios exercise these. None of this is checked against a real PCM yet (#3).

**Stopping or cancelling a read.** A read runs in the background. While it runs, the Case page and the phone's Read OBD show "Reading from the vehicle… N s" with **Stop (keep what was read)** and **Cancel**, and the Read button is disabled; a second read from another device or tab is refused until it ends. **Cancel** stops the read within a fraction of a second, even during the protocol search, closes the adapter's port and records nothing: no event and no measurements, and the page says so. **Stop** ends the read at the next command and records what was read, marked as stopped: trouble-code modes and the freeze frame it did not reach are shown as "not read" (never as "none"), live values it did not reach are listed as "not read, stopped by you", and codes from an earlier read are not marked as gone. A Stop pressed before the vehicle has answered anything has nothing to keep and acts as Cancel. The simulator's `slow_adapter` scenario lets you try both without hardware. Not checked against a real adapter yet (#3).

**Holding the adapter open.** Press **Connect** in the sidebar's "OBD adapter" block (or in the phone page's "OBD adapter connection" section) to keep the adapter open between reads; **Disconnect** closes it, so the MX+ is on the air only when you choose. The block shows the state (Disconnected, Connecting, Connected, Dropped, or the error), the adapter, its battery voltage, how long ago it last answered, and what the last read learned about the vehicle ("not checked" until a read finds out; Connect never asks the vehicle anything). While connected, the app sends the adapter a read-only `ATRV` (battery voltage) every five seconds when no read is running; two missed answers in a row mark the connection **Dropped** and close the port, and **Relink** opens it again (it is manual, nothing reconnects on its own). Reads on the open connection skip the reset and identification, so only the first one does the protocol search. Press Read with no connection and it opens and closes the port itself, as before. A cancelled or failed read leaves the port open and the next read does a full setup. A dropped connection refuses a read until you press Relink or Disconnect. The simulator's `heartbeat_drop` scenario lets you try this without hardware. Not checked against a real adapter yet (#3).

**The log.** Every command and reply the adapter sees, every error and every connection or read event is written to `logs/obd.log` (about 5 MB per file, three files kept) and, without the heartbeat lines, to `sessions/<name>/obd.log` for the active session, including reads that were cancelled or failed. Each line has a time, the session, and `>>` (sent), `<<` (received), `!!` (error) or `**` (event); heartbeat lines are tagged `hb` and simulated traffic `sim`. The **Log** page shows the last 200 lines live, with a Pause toggle and a switch to hide heartbeats. The log is diagnostic: it is never part of the case or the report, and a log that cannot be written never stops a read (the sidebar says so).

### OBD watch

A watch answers one question: does the PCM report RPM while the engine cranks? It records the PCM continuously for
a while (in the background) so a few seconds of cranking are caught, and it needs the standing connection (press
**Connect** first; otherwise it says "Connect to the adapter first."). It lives on the Case page ("Watch the PCM
(OBD-II)"), in the phone's "Watch OBD" section, and in the MCP tools below. It only listens or sends the same
read-only requests as a read; it never transmits on the bus.

- **Raw CAN (default).** Passive monitor: the adapter is put in listen-only mode (`ATCSM1`, so it sends no ACKs) and
  logs every frame it hears. Decoding is best-effort: the one decoder so far reads engine speed from ID `0C9`
  (bytes 1-2, big-endian, times 0.25 rpm). Every decoded value is marked best-effort and carries its source ID and
  a decoder version (`0C9-engine-speed-v1: bytes[1:3] BE x0.25`), which is saved with the watch. Treat it as a
  lead, not a measurement; absence of `0C9` proves nothing by itself. For every saved watch the raw frames are
  kept, one JSON line each, in `sessions/<name>/watches/NNN.jsonl` (with `NNN.json` for the summary), so a better
  decoder can be run on them later (a cancelled watch, or one that recorded nothing, saves no file). The result shows RPM over time and an inventory of the CAN IDs heard. Raw mode needs a CAN vehicle.
- **Poll PIDs.** Reads the PCM's supported-PID maps, then asks for up to six PIDs per request, engine RPM (`010C`)
  in every one and the other supported PIDs (throttle, pedal, commanded throttle, timing advance, MAP, voltage,
  ...) taking turns in the other five slots, about ten requests a second, plus the adapter's own voltage (`ATRV`)
  every 4th cycle. Every value is official OBD data, labelled with its PID (`010C (official)`). If the PCM refuses
  multi-PID requests twice in a row, the watch says so and polls one PID at a time for the rest of the run (RPM
  every other request). Each value is saved as its own line, with the request it came from.

Length is 1 to 120 s (default 20 s). **Stop** ends the watch and keeps what was recorded; **Cancel** throws it
away and saves nothing. A watch that records nothing (for example the key is off, "The vehicle did not answer")
saves nothing. In raw mode, if the CAN stream goes quiet for 1 s the watch ends early as "silent", and keeps
what it has (poll mode has no such rule: each `010C` simply waits for its answer). If the
adapter browns out or resets mid-crank (common while cranking), what was recorded is kept, a note says so, and the
next read re-runs the adapter setup. The same happens after a cancel, an error, or if the adapter's settings
could not be confirmed restored (every restore command must answer `OK`). The simulator can run both modes; simulated watches are flagged `sim`, and a
case holds real or simulated OBD data, never both (watches included).

**Watch OBD during capture.** The Arm panel (laptop and phone) has this checkbox; the laptop also picks the mode
(the phone uses Raw CAN). The watch starts first and the scope is armed only once it is recording; only then does
the page say "Crank the engine now." (Meanwhile the watch panel says "Linked to the capture: crank when the capture
says Armed", never its own crank prompt.) The watch ends 2 s after the capture's recording time ends (or when a capture
that never triggered ends); a cancelled capture keeps its watch. The two are cross-referenced in the case: the
capture lists its watch and the watch its capture, with the trigger time. If the watch cannot start, the capture
runs anyway and the page says why.

**MCP.** Read tools: `watch_progress` (state, mode, whether it is recording, seconds, line and frame counts,
latest and highest RPM with `rpm_source`: best-effort raw decode or official `010C`), `list_watches` and `get_watch` (metadata, summary, ID inventory, the PIDs the watch holds, and a downsampled series for one PID: `pid`, default `0C`). With
"Allow AI control": `obd_watch` (mode, seconds, label), `obd_watch_stop`, `obd_watch_cancel`, and
`arm_capture(watch=true, watch_mode=...)`. After `obd_watch` the assistant waits for `monitoring: true` in
`watch_progress` before asking you to crank.

**OBDLink MX+ caveat.** On the owner's Mac the MX+ answers only the first open of its serial port after Bluetooth
pairing. If the adapter stops answering, forget it in Bluetooth settings and pair it again, then press **Relink** in
the app first. Do not open the port with any other tool (a terminal, a serial monitor) in between: it would use up
the one open. Not checked against a real adapter or PCM yet (#3).

### AI access (MCP)

While the app runs it also serves an MCP endpoint on this computer only (`http://127.0.0.1:8765/mcp`; set
`CRANK_MCP_PORT` for another port, and restart the app after changing code). The sidebar's **AI access** block
shows the URL and the command to add it to Claude Code:

    claude mcp add --transport http ls-swap http://127.0.0.1:8765/mcp

Any MCP client that speaks streamable HTTP can use it. The endpoint is not reachable from the network and has no
password.

**Always available (read and analyze):** `list_sessions`, `get_status`, `get_case_summary`, `get_case_report`,
`list_captures`, `get_capture_analysis` (ranked findings with evidence), `get_capture_signal` (a downsampled
window of one channel; runts survive the downsampling), `compare_captures`, `reanalyze_capture` (a what-if with
different thresholds; nothing is saved), `get_capture_progress`, `obd_progress`, `get_obd_log`, `watch_progress`, `list_watches`, `get_watch`.

**Only while "Allow AI control" is on** (it is off every time the app starts): `arm_capture`, `cancel_capture`,
`obd_connect`, `obd_disconnect`, `obd_relink`, `obd_read`, `obd_stop`, `obd_cancel`, `obd_watch`, `obd_watch_stop`, `obd_watch_cancel`, `add_case_note`,
`set_suspect` and `set_capture_roles` (corrects a saved capture's channel roles and re-analyses it, as the Results
page does). They act on the laptop's current session, mode, scenario and settings. Arming only arms: in
Real-scope mode the assistant is told to ask you to crank. OBD reads keep every existing limit (read-only
requests, never clears codes, real and simulated reads never mix in one case).

Everything the assistant causes is labeled. Captures it arms are labeled `[AI] ...`; notes and suspects it adds
carry `source: "mcp"`, show **[AI]** in the timeline and report, and the report says so. It cannot edit or delete
case entries, delete sessions, change the laptop's mode, scenario or thresholds, connect the scope, or send raw
commands. Turning the toggle off mid-capture does not cancel the capture; it only blocks new control calls.

Not checked against a real scope or adapter yet (#3): the control tools drive the same code the buttons do, and
are tested against the simulator only.

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
- The OBD layer: ELM327 clone quirks and timing, protocol auto-detection on this vehicle, and which modes and PIDs the PSI-flashed E38 answers.

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
    crank/obd/           OBD-II: protocol (pure parsing), pids, transport (interface and simulator), reader,
                         connection (held port and heartbeat), trafficlog (the adapter log)
    crank/mcp_tools.py   the MCP tools: methods over the shared controller (read, gated control and write)
    crank/mcp_server.py  registers the tools and serves them on 127.0.0.1 from inside the app process
    crank/render.py      Markdown and self-contained HTML reports
    crank/procedure.py   the guided test steps as data
    crank/report.py      text report and Plotly figures
    app.py               Streamlit UI (laptop)
    case_ui.py           the Case page
    conn_ui.py           adapter connection status and buttons, the Log page
    mcp_ui.py            the sidebar's AI access block and the Allow AI control toggle
    remote.py            phone remote page
    crank_capture.py     command-line front end
    tests/               pytest suite (no hardware needed)
