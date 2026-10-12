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
crank/obd/protocol.py    pure: build requests, strip ELM noise, parse multi-frame, decode DTCs and PIDs
crank/obd/pids.py        PID table as data: id, name, unit, decode function
crank/obd/transport.py   Transport interface, SimTransport and its scenarios
crank/obd/reader.py      ObdReader, ObdReadout
```

`Transport`: `open()`, `write(str)`, `read_until_prompt(timeout) -> str`, `close()`. Failures raise `ObdError` subclasses (`ObdTimeout`, `ObdNoAdapter`, `ObdProtocolError`).

## Behavior

**Init:** `ATZ`, `ATE0`, `ATL0`, `ATS0`, `ATSP0`. A first `0100` triggers protocol search; `SEARCHING...` is ignored.

**Parsing:** strip echo, prompt and whitespace; recognize `NO DATA`, `UNABLE TO CONNECT`, `BUS INIT: ERROR`, `?`, `STOPPED`; join multi-line CAN responses (`0:`/`1:` indexed frames) and strip the mode-response header (`43`, `47`, `4A`, `42`, `41`). DTC bytes decode to `P`/`C`/`B`/`U` codes.

**Readout (`ObdReadout`):** `codes` (code, status in stored/pending/permanent, description or None), `freeze_frame` (the DTC that caused it plus PID values, or None), `live` (list of name, value, unit, or `supported: false`), `mil_on`, `dtc_count`, `raw` (response per request), `sim` flag, `read_at`.

**Live PIDs read once:** supported-PID bitmaps (0100, 0120, 0140), then RPM (010C), coolant temp (0105), battery/control-module voltage (0142), speed (010D), engine load (0104), short and long fuel trims (0106/0107), throttle (0111), intake air temp (010F). A PID outside the PCM's supported bitmap is not requested and is recorded unsupported.

**Descriptions:** a small built-in table for common codes (including P0335, P0336, P0340, P0341, P0016, P2119, U0100). Unknown codes show "description not recorded".

## Case integration

- One `code_read` event per read: `data = {codes, freeze_frame, mil_on, source: "obd", sim}`. A read with no codes records an empty list; `_validate` currently requires at least one code, so an OBD read with zero codes is recorded as an `observation` ("OBD read: no stored, pending or permanent codes") with `data.source = "obd"`.
- Each supported live value appends a `measurement` (name, value, unit, where "OBD live", conditions "engine off" if RPM is 0 or unsupported, else "engine running"). Unsupported values append nothing.
- Raw responses are stored in the `code_read` event for evidence, not shown in reports.
- Summary: codes carry status; a freeze-frame block is added; simulator reads are labeled simulated. Markdown and HTML renderers show these and escape all text.
- A read that fails (timeout, no adapter, garbage) appends nothing and shows a plain error.

## UI

- Case page: "Read from OBD" with a source choice (Simulator now; real adapters listed as "not available yet"), a scenario picker when simulating, and a result panel showing what was recorded.
- Phone remote: one "Read OBD" button with the same result summary.
- The read runs in the existing controller/thread pattern so the page does not block.

## Testing (no hardware)

- Protocol: recorded byte strings for each mode, echo/noise stripping, multi-frame, `NO DATA`, DTC decoding for all four letters, PID decode formulas against known values.
- Reader against `SimTransport` scenarios: healthy, P2119 stored, MIL on with freeze frame, PCM unsupporting several PIDs, silent adapter, garbage output, mid-read disconnect.
- Events: a read appends the expected events with correct data; a failure appends none; unsupported PIDs yield no measurement; an empty read records the observation.
- Summary/render: codes with status, freeze frame, simulated label, escaping, "not recorded" gaps.
- UI: `AppTest` for the Case page button and the phone button, including the error path.
- Existing tests stay green.

## Not verified against real hardware

ELM327 clone quirks and timing, protocol auto-detection on this vehicle, whether the PSI-flashed E38 answers Modes 02/0A and which PIDs it supports, and any serial or BLE transport.
