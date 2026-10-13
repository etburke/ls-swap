# Serial OBD Transport and Read-Only Allowlist — Design

Date: 2026-10-12

## Context

Slice 1 of the epic "real OBD-II transports and hardening" (issue #4). The OBD reader (`2026-10-11-obd-reader-design.md`) only has `SimTransport`. The adapter under consideration is an OBDLink MX+, which is classic Bluetooth and appears on macOS as a serial port, so a pyserial transport covers it. The owner has no adapter yet, so this slice is built and tested entirely against fakes.

## Purpose and success

The app can read codes, freeze frame and live data through a real classic-Bluetooth ELM327/STN adapter, and it is provably unable to send adapter-NVM-writing or sleep commands.

**Success:** with a fake serial port, `ObdReader` completes a full read through `SerialTransport`; every command the reader sends passes the allowlist; the commands named in #4 are rejected; the Case page and phone remote can choose a real adapter, defaulting to the simulator.

## Out of scope (later slices of #4)

Multi-ECU handling (`ATH1`, header attribution), adapter identification (`STI`/`STDI`/`STSN`) and recording it in the case, the full error taxonomy, sleep/wake handling, `STPX`, batching, `STCSEGR`, reconnect, BLE, deferred minors.

**The first real read must wait for the multi-ECU slice**, as #4 requires: until then a second responding ECU can corrupt a read.

## Design

### Allowlist — `crank/obd/allowlist.py` (pure)

`check(cmd) -> str` returns the normalized command, or raises `ObdForbiddenCommand` (new, subclass of `ObdError`). Normalization: strip, uppercase, remove spaces.

Accepted, nothing else:
- OBD requests: hex strings whose mode byte is 01, 02, 03, 07 or 0A, with the length that mode uses (`03`/`07`/`0A` bare; `01`/`02` followed by a PID, and `02` by a frame byte). Mode 09 is not accepted yet.
- AT setup: `ATZ`, `ATE0`, `ATL0`, `ATS0`, `ATH0`, `ATH1`, `ATSP0`, `ATDPN`, `ATDP`.

It is an allowlist, not a denylist: `ATPP` and every `ST*` command (including `STRSTNVM`, `STSAVCAL`, `STSL*`, `STBT*`, `STW*`, `STSLEEP`) are rejected because they are not listed. Later slices add the specific `ST*` commands they need.

### SerialTransport — `crank/obd/serial_transport.py`

`SerialTransport(port, baud=115200, serial_factory=None)`, a `Transport` with `sim = False`.
- `open()`: opens the port. A missing or busy port raises `ObdNoAdapter` with a plain-language message. `pyserial` is imported lazily; `serial_factory` lets tests inject a fake.
- `send(cmd, timeout)`: calls `allowlist.check` first, before anything touches the port; writes `cmd + "\r"`; reads until the `>` prompt; returns the text before it. No prompt within `timeout` raises `ObdTimeout`. A serial error or closed port mid-read raises `ObdNoAdapter` or `ObdTimeout`; a drop is never swallowed. A lock serializes `send`.
- `close()`: closes the port; safe to call twice.
- Baud: 115200 by default; if `ATZ` gets no prompt at 115200, `open()` retries once at 38400 (older ELM clones).

`SimTransport` does not call the allowlist.

### Port discovery — same module

`list_ports() -> list[tuple[str, str]]` returns `(label, path)` for serial ports that look like adapters (macOS `/dev/cu.OBDLink*`, `/dev/cu.*OBD*`, `/dev/cu.usbserial*`), via `serial.tools.list_ports`. `resolve_port("auto")` returns the single match or raises `ObdNoAdapter` listing the candidates (none, or several); any other value is used as the path.

### Wiring

- `Controller.read_obd(source)`: `source` is `("sim", scenario)` or `("serial", port_or_"auto")`; the default stays the simulator's `healthy`. It builds the transport and runs the existing `ObdReader`. The transport is closed in a `finally`. Errors flow through the existing `ObdError` path; `readout.sim` already marks simulated reads.
- `case_ui.py` and `remote.py`: a "Simulator / Real adapter" choice. Real adapter shows a port dropdown (with "Auto-detect" and a refresh button). Simulator remains the default.
- `requirements.txt`: add `pyserial>=3.5`.

## Testing

- `test_obd_allowlist.py`: each command listed in #4 is rejected; accepted set passes; normalization; a test that runs a full `ObdReader.read()` against a recording transport and asserts every sent command passes `check` (keeps reader and allowlist from drifting).
- `test_obd_serial.py` with a fake serial object: full read through `SerialTransport`; split reads; timeout; unplug mid-read raises and does not return partial text; forbidden command never reaches `write`; 38400 fallback; double close; `list_ports`/`resolve_port` with none, one, several.
- Controller and UI: `read_obd` with a serial source via injected fake factory; simulator default unchanged; existing tests stay green.
- Not covered: reading from a real adapter or truck (needs hardware; stays under #4's "done when").
