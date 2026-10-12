# Bluetooth OBD-II Reader — Design

Date: 2026-10-11

## Context

Piece 2 of the diagnosis-workbench roadmap (see `2026-10-11-case-file-design.md`). It reads trouble codes, freeze frame and a short live-data snapshot from the vehicle's OBD-II port and appends them to the current case. The owner has no adapter yet, so this piece builds and tests the protocol layer and a simulated reader; real transports (classic Bluetooth serial, BLE) are added later behind the same interface.

## Purpose and success

From the Case page or the phone, one tap reads the vehicle and records what the PCM reported. **Success:** a read against the simulator appends a `code_read` event and `measurement` events; the summary and the hand-off report show the codes with their status, the freeze frame, and the live values, and say "not supported" or "not recorded" where the PCM gave nothing.

## Constraints

- Single user, own vehicles, local. Nothing is uploaded.
- Works with no hardware: all protocol logic is pure and tested against byte strings; a `SimTransport` stands in for the adapter.
- Builds on the case file: reads only append events. Existing tests stay green.
- Never invent a value. A PID the PCM does not answer is "not supported", not zero.
- No clear-codes in v1: clearing erases the freeze frame needed for a hand-off.

## Scope

In: ELM327 protocol parsing and request building, Modes 03/07/0A (stored, pending, permanent codes), 02 (freeze frame), 01 (live PIDs), `Transport` interface, `SimTransport` with scenarios, `ObdReader`, event logging, summary and renderer additions, a "Read from OBD" action on the Case page and phone remote.

Out: clearing codes, streaming/graphing live data, serial and BLE transports (interface only, documented), manufacturer-specific PIDs and J1939, code-description database beyond a small built-in table.

## Layout

```
crank/obd/__init__.py
crank/obd/errors.py      ObdError, ObdNoAdapter, ObdTimeout, ObdProtocolError, ObdNotSupported
crank/obd/protocol.py    pure: build requests, strip ELM noise, parse multi-frame, decode DTCs and PIDs
crank/obd/pids.py        PID table as data: id, name, unit, decode function
crank/obd/transport.py   Transport interface, SimTransport and its scenarios
crank/obd/reader.py      ObdReader, ObdReadout
```

`Transport`: `open()`, `send(cmd, timeout) -> str` (the reply up to the `>` prompt; an ELM327 is strictly request/response, so write and read are one call), `close()`. Failures raise `ObdError` subclasses (`ObdTimeout`, `ObdNoAdapter`, `ObdProtocolError`, and `ObdNotSupported` when the PCM answers with a negative response `7F <mode> <code>`).

## Behavior

**Init:** `ATZ`, `ATE0`, `ATL0`, `ATS0`, `ATSP0`. A first `0100` triggers protocol search; `SEARCHING...` is ignored.

**Parsing:** strip echo, prompt and whitespace; recognize `NO DATA`, `UNABLE TO CONNECT`, `BUS INIT: ERROR`, `?`, `STOPPED`; join multi-line CAN responses (`0:`/`1:` indexed frames) and strip the mode-response header (`43`, `47`, `4A`, `42`, `41`). DTC bytes decode to `P`/`C`/`B`/`U` codes.

**Readout (`ObdReadout`):** `codes` (code, status in stored/pending/permanent, description or None), `freeze_frame` (the DTC that caused it plus PID values, or None), `live` (list of name, value, unit, or `supported: false`), `mil_on`, `dtc_count`, `raw` (response per request), `sim` flag, `read_at`.

**Live PIDs read once:** supported-PID bitmaps (0100, 0120, 0140), then RPM (010C), coolant temp (0105), battery/control-module voltage (0142), speed (010D), engine load (0104), short and long fuel trims (0106/0107), throttle (0111), intake air temp (010F). A PID outside the PCM's supported bitmap is not requested and is recorded unsupported. `NO DATA` for a mode means "nothing to report" (no codes, no freeze frame) because a PCM that lacks a mode usually says so with a negative response; the two are never conflated.

**Descriptions:** a small built-in table for common codes (including P0335, P0336, P0340, P0341, P0016, P2119, U0100). Unknown codes show "description not recorded".

## Case integration

- One `code_read` event per read: `data = {codes, freeze_frame, mil_on, source: "obd", sim, protocol, unsupported_modes, raw}`. A clean read records an empty `codes` list. `_validate` allows an empty list only when `source == "obd"` (a manual code entry still needs a code). This matters: the summary treats an OBD read as the complete list the PCM holds, so a later clean read must be able to say "no codes now".
- In the summary, a code seen earlier but absent from a later OBD read keeps its row with status "not in latest OBD read"; manual history codes are never deleted.
- Each supported live value appends a `measurement` (name, value, unit, where "OBD live data", `sim` flag, conditions "OBD, engine running" if the RPM read is above 0, else "OBD, PCM reports 0 RPM or no RPM"; the tool never claims the engine is off, because 0 RPM while cranking is the symptom under investigation). Unsupported values append nothing.
- Raw responses are stored in the `code_read` event for evidence, not shown in reports.
- Summary: codes carry status; a freeze-frame block is added; simulator reads are labeled simulated. Markdown and HTML renderers show these and escape all text.
- A read that fails (timeout, no adapter, garbage) appends nothing and shows a plain error.

## UI

- Case page: "Read from OBD" with a source choice (Simulator now; real adapters listed as "not available yet"), a scenario picker when simulating, and a result panel showing what was recorded.
- Phone remote: one "Read OBD" button with the same result summary.
- A read is synchronous inside a spinner (a simulated read is instant; a job wrapper can come with a real transport). `Controller.read_obd(scenario) -> (readout, None) | (None, message)`.

## Testing (no hardware)

- Protocol: recorded byte strings for each mode, echo/noise stripping, multi-frame, `NO DATA`, DTC decoding for all four letters, PID decode formulas against known values.
- Reader against `SimTransport` scenarios: healthy, P2119 stored, MIL on with freeze frame, PCM unsupporting several PIDs, silent adapter, garbage output, mid-read disconnect.
- Events: a read appends the expected events with correct data; a failure appends none; unsupported PIDs yield no measurement; an empty read records the observation.
- Summary/render: codes with status, freeze frame, simulated label, escaping, "not recorded" gaps.
- UI: `AppTest` for the Case page button and the phone button, including the error path.
- Existing tests stay green.

## Not verified against real hardware

ELM327 clone quirks and timing, protocol auto-detection on this vehicle, whether the PSI-flashed E38 answers Modes 02/0A and which PIDs it supports, and any serial or BLE transport.
