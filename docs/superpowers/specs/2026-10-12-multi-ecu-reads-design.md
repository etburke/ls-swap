# Multi-ECU OBD Reads — Design

Date: 2026-10-12

## Context

Slice 2 of the epic "real OBD-II transports and hardening" (issue #4). It builds on slice 1 (`2026-10-12-serial-obd-transport-design.md`, PR #12). Today the reader runs with headers off (`ATH0`): the adapter hides which ECU answered, and when several ECUs answer one request (for example a transmission controller alongside the PCM) the first answer wins. #4 requires this to be fixed before the first real read.

## Purpose and success

A read attributes every answer to the ECU that sent it. Trouble codes from every ECU are recorded, each tagged with its ECU. Live values, the freeze frame and the supported-PID map come from the PCM only.

**Success:** against a simulated PCM plus transmission, a read records both ECUs' codes with their ECU labels, takes live and freeze-frame values from the PCM, and shows the same code reported by two ECUs as two rows. Reads from a single ECU, and every existing case file, look and behave exactly as before.

## Out of scope

Adapter identification, the full error taxonomy, `STFPA`/`STPX`/`STCSEGR`, 29-bit CAN, non-CAN protocols, per-PID negative-response policy (separate items in #4). Nothing here is validated on real hardware; what the E38 actually answers is tracked in #3.

## Design

### Protocol parsing — `crank/obd/protocol.py` (pure)

- `ecu_messages(resp: str) -> dict[str, bytes] | None`: maps each responding ECU's header (three hex digits such as `7E8`, `7E9`) to its reassembled message. `None` means NO DATA.
- Input is headers-on text: one raw 11-bit CAN frame per line, `HHH B0 B1 …`. ISO-TP is reassembled per ECU:
  - single frame (`0N` + N data bytes);
  - first frame (`1L LL` + data) followed by consecutive frames (`2N` + data), stripped to the declared length;
  - frames from different ECUs may interleave in any order; each ECU's sequence is tracked separately.
- A multi-frame message with missing or out-of-order consecutive frames raises `ObdProtocolError` ("incomplete reply from <ECU>"), never a truncated result.
- Unsupported header shapes raise `ObdProtocolError` with a plain message instead of being misread: 29-bit CAN (header longer than three hex digits) and non-CAN protocols (J1850 / ISO 9141 headers). The target E38 uses 11-bit CAN.
- `response_data_by_ecu(resp, mode, pid=None, frame=False) -> dict[str, bytes] | None`: for each ECU, the data bytes of its message answering this request. Negative responses follow the existing rules per ECU: an ECU answering "not supported" is left out of the result and reported separately (see reader); a busy/pending negative on an ECU raises the existing error naming it. `None` when no ECU has data.
- Existing `messages()`, `response_data()` and their tests are untouched; they stay the headerless path used by the simulator's header-off mode.
- `primary_ecu(headers) -> str`: the PCM is the lowest header among responders, normally `7E8`.

### Reader — `crank/obd/reader.py`

- `INIT` changes `ATH0` to `ATH1` (already on the allowlist).
- The first `0100` identifies responders; `primary_ecu` picks the PCM. If `7E8` did not answer, the lowest responder is used and `ObdReadout.notes` says which, so it is visible, not silent.
- Supported-PID bitmaps (`0100`, `0120`, `0140`), freeze frame (mode 02) and live values (mode 01) use only the PCM's data.
- Modes 03/07/0A: codes are collected from every responding ECU. Each code row gains `"ecu": "7E8"`. An ECU that answers "not supported" for a mode is recorded in `unsupported_modes` entries as `"<mode> (<ecu>)"`; the existing mode-only entries remain for the PCM so old wording is unchanged for single-ECU reads.
- `ObdReadout` gains `ecus: list[str]` (all responders seen), `primary_ecu: str`, `notes: list[str]`; all go into `to_event_data()`. `raw` keeps unparsed text.
- A read with exactly one responding ECU produces the same codes, wording and lines as today.

### Simulator — `crank/obd/transport.py`

- `SimTransport` gets header-on framing alongside the existing header-off path, selected by the last `ATH0`/`ATH1` it was sent. Header-on output is real ISO-TP frames (single, first + consecutive) with interleaving when several ECUs answer.
- `Scenario` gains optional `other_ecus: dict[str, Scenario-like]` (ECU header → stored/pending/permanent codes).
- New scenarios: `pcm_and_trans` (PCM reports P2119, transmission 7E9 reports P0700), `interleaved_multiframe` (both ECUs send multi-frame code lists whose frames interleave), `same_code_two_ecus` (P0335 from both).
- Existing scenarios behave as before; the reader now runs them with headers on, so they are re-verified through the new path.

### Case, summary, report

- `summary.py` keys codes by `(code, ecu)` instead of `code`, so the same code from two ECUs stays two rows. Events without `ecu` use `ecu=""` and behave as today, including the "not in latest OBD read" carry-over logic.
- `render.py` and `summary.py` show the ECU label (for example `P0700 (stored, 7E9)`) only when the read had more than one ECU. A single-ECU read renders exactly as today.
- `case_ui.py`/`ObdReadout.lines()` show the same labels and list the ECUs that responded.

## Testing

- `test_obd_protocol.py`: single frame; multi-frame; two ECUs interleaved; consecutive frame missing or out of order → error; trailing pad bytes stripped; 29-bit and non-CAN rejected with a readable message; negative response from one ECU while another answers; NO DATA; `primary_ecu`.
- `test_obd_reader.py`: `pcm_and_trans` records both ECUs' codes with tags, values from the PCM only; PCM-not-7E8 note; one ECU "not supported" for a mode does not fail the read; unchanged output for every existing scenario.
- `test_summary.py`/`test_obd_case.py`: same code from two ECUs = two rows; events without `ecu` unchanged; label shown only for multi-ECU reads.
- The allowlist drift test (slice 1) keeps covering every command the reader sends.
- Not covered: any real adapter or truck.
