# Adapter Identification — Design

Date: 2026-10-15

## Context

Slice 4 of the epic "real OBD-II transports and hardening" (issue #4). Slices 1 to 3 (PRs #12, #13, #15) added the serial transport and read-only allowlist, per-ECU attribution, and timing and error hardening. The reader still does not know what it is talking to: a genuine STN adapter (OBDLink MX+) and a cheap ELM327 clone look the same to it. The epic asks to identify the adapter, record adapter and firmware in the case for provenance, and gate ST features on a genuine STN adapter. Clones answer `?` to ST commands.

## Purpose and success

Every real read records which adapter produced it, and the reader knows whether ST features are available, so later slices (`STPTO`, `STPX`, `STBC`) can use them safely.

**Success:** against the simulator and a fake serial port, a read against an STN adapter records the ELM version, STN firmware, device name and serial number; a read against an ELM327 clone records the ELM version and "not an STN adapter" without error; an ID command that times out or errors leaves a note and the read still completes. The identity shows in the Read OBD result lines, the code_read event, and one line in the case report and summary. Case files recorded before this slice render exactly as before. No read behaves differently because of what was identified.

## Out of scope

Decided with the owner; each stays an unchecked item in #4:
- Applying anything the identification learns. `STPTO` and per-command timeouts, `STPX`, `STBC`, `STCSEGR`/`STCTOR` are later slices that use the gate built here. Nothing in this slice branches on `supports_st`.
- Adapter voltage (`ATRV`/`STVR`) and passive bus monitoring: issue #8.
- Detecting a clone that fakes an STN reply. A reply of the right shape is treated as STN.
- Nothing here is validated on a real adapter (#3).

## Design

### Adapter data — `crank/obd/adapter.py` (new, pure)

- `AdapterInfo` dataclass: `elm_version: str` (from `ATI`, for example `ELM327 v1.5`), `stn_version: str` (from `STI`, for example `STN2255 v4.3.1`), `device: str` (from `STDI`, for example `OBDLink MX+`), `serial: str` (from `STSN`), `notes: list[str]`. All default to empty.
- `supports_st` (property): true when `stn_version` is set. This is the gate for later slices.
- `label() -> str`: one line.
  - STN: `OBDLink MX+ (STN2255 v4.3.1, serial 123456)`; parts that are unknown are left out.
  - Clone: `ELM327 v1.5 (not an STN adapter, no ST features)`.
  - Nothing identified: `unidentified`.
- `to_dict()` / `from_dict()` for the event data; `from_dict` tolerates missing keys.
- `parse_sti(text) -> str`: the version string when the reply matches `STN<digits> v<digits>.<digits>[.<digits>]` (case-insensitive, after the usual line cleaning), otherwise an empty string. `?`, `NO DATA`, an echo, or junk all give empty, meaning not STN.
- `clean_id(text) -> str`: the first non-empty line of an ID reply with echo and prompt noise removed; `?` gives empty.

### Allowlist — `crank/obd/allowlist.py`

- `ATI` joins `AT_ALLOWED`.
- `STI`, `STDI` and `STSN` are added as exact matches in a separate `ST_ALLOWED` set. Every other `ST*` command stays refused. The tests that reject `STSLEEP`, `STSAVCAL`, `STRSTNVM`, `STSL*`, `STBT*`, `STW*`, `ATPP` stay and must keep passing.

### Reader — `crank/obd/reader.py`

- After `INIT` and before the first `0100`, the reader runs `_identify()`:
  1. Send `ATI`. A non-empty clean reply is the ELM version.
  2. Send `STI`. If `parse_sti` returns a version, the adapter is STN: record it, then send `STDI` and `STSN` and record their cleaned replies. Otherwise stop: a plain ELM327.
- Identification is best-effort. A timeout (`ObdTimeout`) or `ObdAdapterError` on an ID command adds a note to `AdapterInfo.notes` (for example "Adapter identification incomplete: STSN timed out.") and the step continues with what it has. Other errors (the adapter is gone, `ObdNoAdapter`) propagate as they do today. ID commands are not retried.
- Identification runs once per read. After an `LV RESET` re-init it is not repeated.
- `ObdReadout` gains `adapter: AdapterInfo`; `to_event_data()` gains `"adapter": adapter.to_dict()`. Nothing else in the read changes. A non-empty `adapter.notes` is copied into `ObdReadout.notes` so it shows in the result lines.
- `ObdReadout.lines()` adds `Adapter: <label>` after the codes line. For a simulated read the label carries the existing `(simulated)` marker.

### Simulator — `crank/obd/transport.py`

- `Scenario` gains `adapter: str = "stn"`, naming a preset:
  - `stn`: `ATI` → `ELM327 v1.5`, `STI` → `STN2255 v4.3.1`, `STDI` → `OBDLink MX+ 1234`, `STSN` → `110012345678`.
  - `elm327`: `ATI` → `ELM327 v1.5`; every `ST*` command → `?`.
  - `flaky_id`: as `stn`, but `STSN` raises `ObdTimeout`.
- All existing scenarios default to `stn`, so their reads gain an adapter and are otherwise unchanged. New scenarios: `elm_clone` and `flaky_adapter_id`.
- The simulator's own command checking accepts the new commands, as for any command the reader sends.

### Case, summary, report

- `summary.py` carries the adapter of the latest non-simulated OBD read (`adapter_label`), if the event has an `adapter` dict. Events without it leave it empty.
- `render.py` (text and HTML) shows `Adapter: <label>` once, near the OBD codes, only when `adapter_label` is set. Simulated reads never set it, since the simulated identity is not evidence. A case with no real read, or one recorded before this slice, renders exactly as before.
- The codes line in the summary and the existing wording for single- and multi-ECU reads are unchanged.

## Testing

- `test_obd_adapter.py` (new): `parse_sti` for a real reply, `?`, `NO DATA`, junk and an echoed command; `clean_id`; `label()` for STN, clone and unidentified; `supports_st`; `to_dict`/`from_dict` round trip and a missing-key dict.
- `test_obd_allowlist.py`: `ATI`, `STI`, `STDI`, `STSN` pass in any case and spacing; `STSLEEP`, `STSAVCAL`, `STRSTNVM`, `STSL…`, `STBT…`, `STW…`, `STPTO`, `STIX` and `ATPP` are refused. The drift test still covers every command the reader sends, including the ID commands.
- `test_obd_reader.py`: the `stn` preset records all four fields and `supports_st`; `elm_clone` records the ELM version, no ST fields, no error, and sends no `STDI`/`STSN`; `flaky_adapter_id` completes the read with a note; an `ObdNoAdapter` during identification still fails the read; an `LV RESET` mid-read does not repeat identification; every existing scenario still produces the same codes, values and notes as before; the event data carries the adapter.
- `test_summary.py` / `test_render.py`: a real read's adapter line appears once; a simulated read shows none; an old event without `adapter` is unchanged.
- Not covered: a real adapter, a real clone's quirks, firmware strings other than those in the presets.
