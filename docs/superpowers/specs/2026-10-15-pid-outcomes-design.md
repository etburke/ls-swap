# PID Outcomes and Report Presentation — Design

Date: 2026-10-15

## Context

Slice 5 of the epic "real OBD-II transports and hardening" (issue #4). Slices 1 to 4 (PRs #12, #13, #15 and the identification slice) added the serial transport, per-ECU attribution, timing and error hardening, and adapter identification. What is left before a first real read is how the reader and the report treat the ordinary surprises of a real PCM, which a clean simulator never produces:

- A negative response on the `0101` status request or on a support-map request (`0120`, `0140`) fails the whole read. A negative response from the PCM on the first `0100` raises a bare `KeyError`.
- A busy reply (`21`, `22`, `78`) on any request fails the whole read with "try again", and so does a busy reply from a non-PCM ECU on a mode-01 PID even though the PCM answered.
- `NO DATA` on a PID the support map listed is shown the same as "not supported".
- Readout notes (retries, resets, adapter identification gaps, "PCM not on 7E8") are stored and shown in the live result lines, but the case summary and report do not render them.
- A code that is both stored and permanent collapses into one summary row.
- A simulated read can demote or overwrite what a real read recorded.
- `ATDPN` is read without the line cleaner.

## Purpose and success

A first read on a real E38 finishes with what the PCM gave and says plainly what it did not, and the case report shows that account.

**Success:** against the simulator and fakes, a read in which the PCM refuses, is busy on, or does not answer one PID completes, records a reason for each missing value, and shows the reasons and the read's notes in the result lines, the case summary and the report. Stored and permanent codes get separate rows. A case never holds both simulated and real OBD reads. A clean read, and every case file recorded before this slice, looks and behaves as before.

## Out of scope

Decided with the owner; each stays an item in #4:
- The headerless parser (`messages()`, `response_data()`) and its minor (hex multi-frame indices `A:`–`F:`, silent truncation). Nothing on the read path calls it since slice 2, only tests do. It stays unfixed and unchecked, with a note that it is test-only code that can be deleted instead.
- Mode 03/07/0A negative-response behaviour is unchanged: a busy ECU there still fails the read, and "not supported" is still listed per ECU.
- Simulated scope captures stay as they are. The one-kind rule covers OBD reads only.
- Nothing here is validated on a real PCM (#3).

## Design

### Per-PID outcomes — `protocol.py`, `reader.py`

- `LiveValue` gains `reason: str = ""`. Empty means the value was read. Otherwise one of:
  - `not_in_map`: the PCM's support map does not list the PID; nothing was sent.
  - `refused`: the PCM sent a not-supported negative response (`11`, `12`, `31`) or any other negative response that is not a busy one.
  - `busy`: the PCM sent `21`, `22` or `78` and again on the one retry.
  - `no_answer`: `NO DATA`, or a reply too short to decode.
- `supported` stays true when `value` is not None.
- Protocol layer: a new function reports a negative response per ECU without raising, `{ecu: nrc}`, for PID-level requests. `response_data_by_ecu` keeps its signature and its raising behaviour for modes 03/07/0A. Only the PCM's reply counts for a mode 01/02 PID request: a negative response from any other ECU on such a request is ignored.
- Reader: one helper asks the PCM for a PID and returns `(data | None, reason)`. It is used for `0101`, the support maps, the freeze-frame PIDs and the live PIDs.
- A `busy` reply is retried once after `retry_pause`, through the same path and the same per-read cap (`MAX_RETRIES = 3`) as transient adapter errors, and is noted ("Retried 010C: the PCM was busy."). Once the cap is spent, further busy PIDs are marked `busy` without a retry.
- `0101` failing sets `mil_on` to `None` and adds a note ("Check engine light status unavailable: the PCM refused 0101.").
- If a support-map request (`0120`, `0140`) fails, the read notes it and tries every PID in that map's range anyway; each is reported by its own outcome. Such a PID is never labelled `not_in_map`.
- Freeze-frame values that fail are kept in `freeze_frame["unavailable"]` as `{"name", "reason"}`; the successful ones stay in `values`.
- The first `0100`: a negative response from the PCM, or no PCM data, raises `ObdProtocolError("The PCM refused the supported-PID request (negative response XX).")` or "The PCM sent no supported-PID map.", never a `KeyError`.

### Presentation — `reader.py`, `case.py`, `summary.py`, `render.py`

- `ObdReadout.lines()`: "Live values read: N of M" followed by the unavailable PIDs grouped by reason, in plain words: "not in the PCM's support map", "refused by the PCM", "PCM busy", "no answer". The `not_in_map` group is listed last.
- `ObdReadout.to_event_data()` gains `"unavailable": [{"name", "reason"}]` for the live PIDs. Event data without the key reads as none.
- `summary.py`: `Summary.obd_notes: list[str]` (the latest OBD read's notes) and `Summary.obd_unavailable: list` (its unavailable PIDs other than `not_in_map`, with reasons). `render.py` prints both under Key data as "OBD read notes" and "Not read from the PCM". They appear only when non-empty, so a clean read renders as before.
- Summary codes are keyed by `(code, ecu, status)`. A code that is stored and permanent shows as two rows, and the "not in latest OBD read" demotion applies to each row separately. Events with no `status` use an empty status.
- A code's status text stays `(stored)` or `(permanent, 7E9)` as today.

### One kind per case — `case.py`, `controller.py`

- `Case.obd_kinds() -> set[str]`: `"real"` and/or `"simulated"` for the OBD `code_read` events in the case.
- `Controller.read_obd` builds the transport, then, before opening it: if the new read's kind is not in the case's kinds and the case has any kind, it returns `(None, "This case already has <real|simulated> OBD reads. Start a new case to use the <simulator|real adapter>.")` and records nothing. Case files that already mix both are left as they are and keep rendering with the `[SIMULATED]` markers; reads of either kind into them are refused.

### `ATDPN` — `reader.py`

- The reply goes through `clean_id` before `is_can`. An empty result raises `ObdProtocolError("The adapter did not report its protocol.")`.

### Simulator — `transport.py`

- `Scenario` gains `pid_nrc: dict` mapping a request (for example `"010C"`) to `{"nrc": 0x22, "times": 1}`, the PCM answering that request with `7F <mode> <nrc>` that many times (99 = always), and a `no_answer_pids` set for requests that draw `NO DATA` even though the map lists them. `other_ecus` can also set `pid_nrc` so a non-PCM ECU's reply on a mode-01 PID can be exercised.
- New scenarios: `pid_refused` (not-supported NRC on one live PID), `pid_busy_once`, `pid_busy_always`, `pid_no_answer`, `status_refused` (`0101`), `map_refused` (`0120`), `pcm_refuses_0100`, `trans_busy_on_pid` (the transmission controller busy on a mode-01 PID while the PCM answers).

## Testing

- `test_obd_protocol.py`: the per-ECU negative-response function (not supported, busy, other, positive, mixed ECUs); `response_data_by_ecu` unchanged for modes 03/07/0A.
- `test_obd_reader.py`: each new scenario completes with the right `reason`; busy-once is retried and noted and ends with a value; busy-always ends `busy`; the retry cap holds across several busy PIDs; a failing `0101` gives `mil_on is None` and a note; a failing `0120` tries the PIDs in its range; `pcm_refuses_0100` raises the clear message; `trans_busy_on_pid` leaves the read intact; freeze-frame failures land in `unavailable`; `ATDPN` with an echo or blank line still parses; every existing scenario produces the same codes, values and notes as before.
- `test_obd_case.py` / `test_summary.py` / `test_render.py`: notes and unavailable PIDs appear in the summary and both reports and are absent for a clean read; a stored-and-permanent code shows two rows and demotes separately; events without the new keys render unchanged.
- `test_obd_controller.py`: a real read into a simulated case and the reverse are refused with the message, before the adapter is opened and with nothing recorded; the first read into an empty case of either kind succeeds; a mixed legacy case refuses both; the lock is released after a refusal.
- The allowlist drift test still covers every command the reader sends.
- Not covered: a real PCM's actual negative-response behaviour (#3).
