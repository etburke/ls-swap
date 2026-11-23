"""WatchJob: record the PCM continuously while the owner cranks, as raw CAN monitor lines or 010C polling."""
from __future__ import annotations

import statistics
import threading
import time
from typing import Callable

from . import protocol as P
from .adapter import clean_id, parse_voltage
from .canframes import DECODERS, decode, parse_line
from .errors import ObdAdapterError, ObdCancelled, ObdMonitorStuck, ObdProtocolError, ObdVehicleSilent
from .pids import PIDS, POLL_PIDS, poll_groups
from .reader import INIT

MODES = ("raw", "poll")
MAX_SECONDS = 120.0
DEFAULT_SECONDS = 20.0
FIRST_TIMEOUT = 20.0                     # the first OBD request may start a protocol search
POLL_PERIOD = 0.1                        # shortest poll cycle, so a fast adapter does not flood the file
ATRV_EVERY = 4
MONITOR_GRACE = 0.2                      # ATMA counts as accepted after this long with no error
SILENT_TEXT = "The vehicle did not answer. Is the key on?"
END_NOTES = {
    "buffer_full": "The adapter's buffer overflowed (BUFFER FULL); frames after that point were lost.",
    "reset": "The adapter reset during the watch; its settings were re-applied.",
    "silent": "No CAN traffic for 1 s; the watch ended early.",
    "prompt": "The adapter ended monitor mode on its own.",
    "single_pid": "The PCM refused multi-PID requests; polled one PID at a time (RPM every other request).",
}


def _median_nonzero(values: list[float]) -> float | None:
    nz = [v for v in values if v]
    return statistics.median(nz) if nz else None


def _first_nonzero_s(samples: list[tuple[float, float]], started: float) -> float | None:
    return next((round(t - started, 3) for t, v in samples if v), None)


def summarize_raw(rows: list[dict], started: float) -> dict:
    frames = [r for r in rows if "id" in r]
    span = max((r["t"] for r in rows), default=started) - started
    ids: dict = {}
    for r in frames:
        ids[r["id"]] = ids.get(r["id"], 0) + 1
    gaps = [b["t"] - a["t"] for a, b in zip(frames, frames[1:])]
    speed = [(r["t"], d) for r in frames for d in r.get("decoded", []) if d["name"] == "Engine speed"]
    engine = None
    if speed:
        values = [d["value"] for _, d in speed]
        engine = {"decoder": speed[0][1]["decoder"], "best_effort": True, "samples": len(speed),
                  "max": max(values), "median_nonzero": _median_nonzero(values),
                  "first_nonzero_s": _first_nonzero_s([(t, d["value"]) for t, d in speed], started)}
    return {"lines": len(rows), "frames": len(frames),
            "ids": {i: {"count": n, "hz": round(n / span, 1) if span > 0 else 0.0} for i, n in sorted(ids.items())},
            "max_gap_s": round(max(gaps), 3) if gaps else None, "engine_speed": engine}


def summarize_poll(rows: list[dict], started: float) -> dict:
    rpm = [r for r in rows if r["cmd"] == "010C"]
    answered = [(r["t"], r["value"]) for r in rpm if r["value"] is not None]
    volts = [r["value"] for r in rows if r["cmd"] == "ATRV" and r["value"] is not None]
    values = [v for _, v in answered]
    pids: dict = {}
    for r in rows:
        cmd = r.get("cmd", "")
        if len(cmd) != 4 or not cmd.startswith("01"):
            continue
        pid = int(cmd[2:], 16)
        e = pids.setdefault(cmd[2:], {"name": PIDS[pid].name if pid in PIDS else cmd, "unit": r.get("unit", ""),
                                      "answered": 0, "min": None, "max": None})
        v = r.get("value")
        if v is not None:
            e["answered"] += 1
            e["min"] = v if e["min"] is None else min(e["min"], v)
            e["max"] = v if e["max"] is None else max(e["max"], v)
    return {"samples": len(rpm),
            "rpm": {"source": "010C (official)", "answered": len(answered), "max": max(values, default=None),
                    "median_nonzero": _median_nonzero(values), "first_nonzero_s": _first_nonzero_s(answered, started)},
            "voltage": {"min": min(volts), "max": max(volts)} if volts else None, "pids": pids}


class _Interrupt:
    def __init__(self, job: "WatchJob"):
        self.job = job

    def is_set(self) -> bool:
        return self.job._interrupted()


class WatchJob:
    def __init__(self, transport, mode: str, writer, *, seconds: float | None,
                 until: Callable[[], bool] | None = None, ceiling: float = MAX_SECONDS, cold: bool = False,
                 sim: bool = False, label: str = "", adapter: str = "", session_name: str = "",
                 on_finish: Callable[[], None] | None = None, on_saved: Callable[["WatchJob"], None] | None = None):
        if mode not in MODES:
            raise ValueError(f"unknown watch mode {mode!r}")
        self.t, self.mode, self.writer = transport, mode, writer
        self.limit = ceiling if seconds is None else min(seconds, MAX_SECONDS)
        self.until, self.cold, self.sim, self.label, self.adapter = until, cold, sim, label, adapter
        self.linked = until is not None               # a capture-linked watch: the capture says when to crank
        self.session_name, self._on_finish, self._on_saved = session_name, on_finish, on_saved
        self.cancel_event, self.stop_event = threading.Event(), threading.Event()
        self._halt = threading.Event()                # stop, cancel or until: end the stream / poll loop
        self.monitoring = threading.Event()
        self.state, self.message, self.meta = "idle", "", None
        self.lines = self.frames = 0
        self.latest_rpm: float | None = None
        self.max_rpm: float | None = None
        self.restore_failed = False
        self.error: Exception | None = None           # what ended the watch early, if anything raised
        self.interrupted = False                      # a command was cut short (cancel, or stop before data)
        self.outcome = ""                             # the final state, set just before on_finish runs
        self.protocol, self.notes, self._rows = "", [], []
        self.started_at = self.recording_at = self.finished_at = 0.0
        self._changed = self._reset = self._until_hit = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.started_at = time.time()
        self.state = "running"
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """End the watch and keep what was recorded."""
        self.stop_event.set()
        self._halt.set()

    def cancel(self) -> None:
        """Throw the watch away: nothing is saved."""
        self.cancel_event.set()
        self._halt.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def reset(self) -> bool:
        """The adapter reset during the watch (its settings were re-applied, or the attempt failed)."""
        return self._reset

    @property
    def seconds(self) -> float:
        if not self.started_at:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    # recording
    def _record(self, row: dict) -> None:
        self.writer.record(row)
        self._rows.append(row)
        self.lines += 1

    def _check_until(self) -> None:
        if self.until is not None and not self._halt.is_set() and self.until():
            self._until_hit = True
            self._halt.set()

    def _interrupted(self) -> bool:
        """What the transport polls while it waits: a cancel, or a stop/until before anything was recorded
        (a slow protocol search must not hold up a stop; there is nothing to keep then)."""
        if self.cancel_event.is_set():
            return True
        if self.lines:
            return False
        self._check_until()
        return self._halt.is_set()

    def _send(self, cmd: str, timeout: float = 5.0) -> str:
        if self._interrupted():
            raise ObdCancelled()
        return self.t.send(cmd, timeout)

    def _vehicle_check(self) -> None:
        if self.cold:
            for cmd in INIT:
                self._send(cmd)
        resp = self._send("0100", FIRST_TIMEOUT)
        try:
            try:
                msgs = P.ecu_messages(resp)              # headers on (the connection's setup)
            except ObdAdapterError:
                raise
            except ObdProtocolError:
                msgs = P.messages(resp)                  # headers off (after an unnoticed adapter reset)
        except ObdAdapterError as e:
            if e.kind in ("UNABLE TO CONNECT", "BUS INIT"):
                raise ObdVehicleSilent(SILENT_TEXT) from e
            raise
        if not msgs:
            raise ObdVehicleSilent(SILENT_TEXT)

    def _run_raw(self) -> str:
        self._vehicle_check()
        self.protocol = clean_id(self._send("ATDPN"), "ATDPN")
        if not P.is_can(self.protocol):
            raise ObdProtocolError(f"Raw watch needs a CAN vehicle; this one answered on protocol {self.protocol}.")
        if self._interrupted():                      # never enter monitor mode only to leave it at once
            raise ObdCancelled()
        self._send("ATH1")
        self._changed = True
        for cmd in ("ATCSM1", "ATCAF0", "ATD1"):
            self._send(cmd)
        accepted = threading.Timer(MONITOR_GRACE, self.monitoring.set)
        self.recording_at = time.time()
        accepted.start()
        try:
            end = self.t.stream("ATMA", self._on_line, self._halt, self.limit)
        finally:
            accepted.cancel()
            accepted.join()
        self._reset = end.reason == "reset"
        if not end.at_prompt:                        # no prompt after the stop byte: it may still be monitoring
            self.restore_failed = True
        return end.reason

    def _on_line(self, t: float, text: str) -> None:
        row = {"t": t, "raw": text}
        frame = parse_line(text)
        if frame is not None:
            values = decode(frame)
            row.update(id=frame.can_id, dlc=frame.dlc, data=frame.data.hex().upper(),
                       decoded=[{"name": d.name, "value": v, "unit": d.unit, "decoder": d.version,
                                 "best_effort": True} for d, v in values])
            self.frames += 1
            for d, v in values:
                if d.name == "Engine speed":
                    self.latest_rpm, self.max_rpm = v, max(v, self.max_rpm or 0.0)
        self._record(row)
        self.monitoring.set()
        self._check_until()

    def _support_map(self, timeout: float) -> set[int]:
        """The PIDs the PCM advertises (0100, then 0120/0140/0160 while each map flags the next). An unreadable
        first map means RPM alone is polled, as before."""
        supported: set[int] = set()
        for base in (0x00, 0x20, 0x40, 0x60):
            if base and base not in supported:
                break
            try:
                r = P.ecu_reply(self._send(f"01{base:02X}", timeout), 0x01, base, ecu="7E8")
            except ObdProtocolError:
                r = None
            except ObdAdapterError as e:
                if not e.transient:
                    self._reset = e.reset
                    raise
                r = None
            timeout = 5.0
            if r is None or r[1] is not None:
                break
            supported |= P.supported_pids(base, r[0])
        return supported

    def _poll(self, pids: tuple[int, ...], t: float, timeout: float) -> str:
        """Send one request for `pids` and record one row per PID. Returns "ok", "busy", or "refused" (a
        negative answer, no answer, an unreadable reply, or a multi-PID reply without RPM)."""
        req, raw, data, reason = P.request_multi(pids), "", None, ""
        try:
            raw = self._send(req, timeout)
            msgs = P.ecu_messages(raw)
            m = msgs.get("7E8") if msgs else None
            if not m:
                reason = "no_answer"
            elif m[0] == 0x7F and len(m) >= 3:
                reason = "busy" if m[2] in P.BUSY_NRC else "refused"
            elif m[0] == 0x41:
                data = P.split_multi(m[1:], pids)
            else:
                reason = "no_answer"
        except ObdProtocolError:
            reason = "no_answer"
        except ObdAdapterError as e:
            if not e.transient:
                self._reset = e.reset
                raise
            reason = "adapter_error"
        for pid in pids:
            value, why = None, reason
            if data is not None:
                dec = P.decode_pid(pid, data[pid]) if pid in data else None
                value, why = (dec[0], "") if dec else (None, "no_answer")
            if pid == 0x0C and value is not None:
                self.latest_rpm, self.max_rpm = value, max(value, self.max_rpm or 0.0)
            self._record({"t": t, "cmd": f"01{pid:02X}", "req": req, "raw": raw.strip(), "value": value,
                          "unit": PIDS[pid].unit, "reason": why})
        if reason == "busy":
            return "busy"
        if reason or (len(pids) > 1 and 0x0C not in (data or {})):
            return "refused"
        return "ok"

    def _run_poll(self) -> str:
        if self.cold:
            for cmd in INIT:
                self._send(cmd)
        self._send("ATH1")                           # PID replies are parsed by header; a reset clears it
        supported = self._support_map(FIRST_TIMEOUT)
        groups = poll_groups(supported)
        others = [p for p in POLL_PIDS if p in supported]
        self.recording_at = time.time()
        deadline, n, refusals, single, k = time.monotonic() + self.limit, 0, 0, len(groups[0]) == 1, 0
        while True:
            self._check_until()
            if self._halt.is_set():
                return "stopped"
            cycle = time.monotonic()
            if cycle >= deadline:
                return "time"
            if single:                                # RPM every other request, the others in turn between
                pids = (0x0C,) if (k % 2 == 0 or not others) else (others[(k // 2) % len(others)],)
                k += 1
            else:
                pids = groups[n % len(groups)]
            verdict = self._poll(pids, time.time(), FIRST_TIMEOUT if n == 0 else 5.0)
            if not single and len(pids) > 1:
                refusals = refusals + 1 if verdict == "refused" else 0
                if refusals >= 2:
                    single = True
                    self.notes.append(END_NOTES["single_pid"])
            if self.latest_rpm is not None and any(r["cmd"] == "010C" and r["value"] is not None
                                                   for r in self._rows[-len(pids):]):
                self.monitoring.set()                # the PCM is answering 010C: recording for real
            n += 1
            if n % ATRV_EVERY == 0:
                raw = self._send("ATRV")
                volts = parse_voltage(raw)
                self._record({"t": time.time(), "cmd": "ATRV", "raw": raw.strip(), "value": volts, "unit": "V",
                              "reason": "" if volts is not None else "no_answer"})
            self._halt.wait(max(0.0, min(cycle + POLL_PERIOD, deadline) - time.monotonic()))

    def _restore(self) -> None:
        """Leave the adapter as a read expects it: out of monitor mode (the stream did that) with its settings
        back, or re-initialised after a reset. Every reply must confirm (OK, or the banner for ATZ): an adapter
        still monitoring answers a command with STOPPED instead. A failure is flagged so the connection can be
        marked cold."""
        self.t.cancel = None
        if not (self._changed or self._reset):
            return
        try:
            for cmd in INIT if self._reset else ("ATCAF1", "ATD0"):
                reply = self.t.send(cmd, 5.0)
                if ("ELM327" if cmd == "ATZ" else "OK") not in reply.upper():
                    self.restore_failed = True
        except Exception:
            self.restore_failed = True

    def _work(self) -> None:
        state, end_reason, error = "error", "", None
        self.t.cancel = _Interrupt(self)
        try:
            end_reason = self._run_raw() if self.mode == "raw" else self._run_poll()
        except ObdCancelled:
            self.interrupted = True
            if not self.cancel_event.is_set():
                end_reason = "stopped"               # stop or until before anything was recorded
        except Exception as e:                       # the link dropped, a timeout, or a bug: kept if lines came
            error, end_reason = e, "error"
            if isinstance(e, ObdMonitorStuck):
                self.restore_failed = True
        self.error = error
        self._restore()
        if end_reason == "stopped" and self._until_hit:
            end_reason = "until"
        try:
            if self.cancel_event.is_set():
                state, self.message = "cancelled", "Watch cancelled; nothing was saved."
                self.writer.discard()
            elif error is not None or end_reason in END_NOTES:
                text = (str(error) or type(error).__name__) if error is not None else END_NOTES[end_reason]
                if not self.lines:                   # nothing recorded: an error, and nothing saved
                    self.message = text
                    self.writer.discard()
                else:
                    self.notes.append(text)
                    state = self._save("stopped", end_reason)
            else:
                state = self._save("done" if end_reason == "time" else "stopped", end_reason)
        except Exception as e:                       # a storage failure while saving
            state, self.message = "error", f"{type(e).__name__}: {e}"
        finally:
            self.outcome = state
            try:
                if self._on_finish:
                    self._on_finish()                # free the controller's lock before anyone sees the end
            except Exception as e:
                if not self.message:
                    self.message = f"{type(e).__name__}: {e}"
            finally:
                self.finished_at = time.time()
                self.state = state

    def _save(self, state: str, end_reason: str) -> str:
        if self.restore_failed:
            self.notes.append("The adapter's settings could not be restored; it will be set up again "
                              "before the next read.")
        started = self.recording_at or self.started_at
        ended = time.time()
        summarize = summarize_raw if self.mode == "raw" else summarize_poll
        self.meta = {"id": self.writer.id, "label": self.label, "mode": self.mode, "sim": self.sim,
                     "adapter": self.adapter, "protocol": self.protocol, "started": started, "ended": ended,
                     "seconds": round(ended - started, 3), "end_reason": end_reason, "notes": list(self.notes),
                     "summary": summarize(self._rows, started), "capture_id": None, "trigger_t": None,
                     "decoders": [d.version for d in DECODERS.values()] if self.mode == "raw" else []}
        self.writer.finish(self.meta)
        if self._on_saved:
            try:
                self._on_saved(self)
            except Exception as e:                   # the watch is saved; say what went wrong after
                self.message = f"Saved, but {type(e).__name__}: {e}"
        return state
