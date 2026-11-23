# OBD: more PIDs, multi-PID polling, and watch charts

## Intent

The truck cranks with a clean crank signal and the PCM reports RPM, fires coil 1 and injector 1, yet it does not
start. To see what the PCM is doing during a crank, the owner wants the app to collect every useful standard PID
(throttle, pedal, commanded throttle, timing advance, MAP, voltage, ...), not just RPM, and to graph any of them
over time on a page where saved watches can be reopened, with the scope's RPM overlaid for a linked capture.

## Goals

- **Watches page**: a new sidebar page listing the session's saved OBD watches. Selecting one shows its summary and
  charts. The Case page's OBD section keeps its controls and shows the just-finished watch the same way.
- **Charts**: pick which PIDs to plot (default: RPM). One plot per unit, stacked with a shared time axis, never two
  y-scales on one plot. RPM plot overlays the linked capture's per-revolution crank RPM from CH1, and marks the
  scope trigger. Time is seconds from the start of recording.
- **Poll watch collects many PIDs** using multi-PID requests (owner's pick, option 1): up to 6 PIDs per mode 01
  request on CAN, RPM (`0C`) in every request, the other PIDs rotated through the remaining 5 slots, cycle about
  100 ms as today. Only PIDs the PCM advertises (0100/0120/0140/0160 bitmaps, read at watch start) are asked.
- **Fallback**: if the PCM refuses or garbles a multi-PID request, the watch switches for the rest of the run to
  single-PID requests, RPM every other request and one other PID in rotation between (option 2), and notes it.
- **Read collects every supported known PID** (the table below), not the fixed 10, plus codes and freeze frame as
  today.
- **MCP**: `get_watch` gains an optional `pid` (default `0C`) returning that PID's series; the response lists which
  PIDs the watch holds.

## PID table (mode 01, standard SAE J1979 decodes)

Existing: `04` load, `05` coolant, `06`/`07` fuel trims B1, `0B` MAP, `0C` RPM, `0D` speed, `0F` IAT, `11` throttle,
`42` module voltage. Added: `08`/`09` fuel trims B2, `0E` timing advance, `10` MAF, `1F` run time since start,
`33` barometric pressure, `43` absolute load, `44` commanded equivalence ratio, `45` relative throttle, `46`
ambient air temperature, `47` throttle B, `49`/`4A` accelerator pedal D/E, `4C` commanded throttle actuator.
Anything the PCM advertises outside the table is not requested (no decode to show).

## Constraints

- Read-only stays absolute. The allowlist changes from `01` + one PID to `01` + one to six PIDs; nothing else.
- CAN 11-bit only, as today. Multi-PID replies may span several CAN frames; the existing ISO-TP reassembly handles
  them. The reply is `41` then (PID, data) pairs in any order; it is split using the table's byte counts, and a PID
  missing from the reply is recorded as `no_answer`.
- Saved-watch format stays readable by old code: one row per PID value, `cmd` = `01XX` for that PID, plus a shared
  `req` field holding the actual request. Existing summaries keyed on `010C` keep working. Old watches (RPM only)
  open on the new page unchanged.
- No dual axes; plots use the existing plotly dependency.

## Approaches considered

1. Multi-PID requests with single-PID fallback (**chosen**): fastest updates for every PID with RPM in every
   request; costs one allowlist extension and a reply splitter.
2. Single-PID rotation: no allowlist change, but each non-RPM PID updates only every ~3 s.
3. Owner picks a few PIDs per watch: fast but needs a decision before every crank.

## Non-goals

- GM enhanced data (mode 22), raw-mode decoding changes, writing anything to the PCM, live charts while a watch runs
  (the running indicator stays as is).

## Builder's calls

PID grouping and rotation order, how charts are laid out on the page, test fixtures, sim adapter support for
multi-PID replies (needed for tests and the simulator), and wording.
