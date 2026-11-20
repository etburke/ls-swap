"""Shared server-side state so a phone session and the laptop session drive the same capture."""
from __future__ import annotations

import os
import socket
import threading
from pathlib import Path

from .capture import CaptureJob
from .case import Case
from .model import CaptureSettings, Thresholds
from .obd.connection import ObdConnection
from .obd.errors import ObdAdapterError, ObdCancelled, ObdError, ObdVehicleSilent
from .obd.job import ObdJob
from .obd.reader import ObdReader
from .obd.serial_transport import SerialTransport, resolve_port
from .obd.trafficlog import LoggingTransport, TrafficLog
from .obd.transport import HeldTransport, SimTransport
from .scope import RigolScope, SimScope
from .store import SessionStore

SYNCABLE = ("mode", "scenario", "session_name", "settings", "thresholds")
_SEVERITY_ORDER = {"fail": 0, "warn": 1, "info": 2}


class Controller:
    def __init__(self, store: SessionStore, poll_s: float = 0.2, heartbeat_s: float = 5.0, logs_dir=None):
        self.store, self.poll_s = store, poll_s
        self.mode = "Simulator"
        self.scenario = "healthy"
        self.session_name = "default"
        self.settings = CaptureSettings()
        self.thresholds = Thresholds()
        self.scope: RigolScope | None = None      # real scope, set by the laptop's Connect button
        self.sim_fault: str | None = None         # simulator fault injection (tests)
        self.job: CaptureJob | None = None
        self._lock = threading.Lock()
        self._obd_lock = threading.Lock()
        self.obd_job: ObdJob | None = None
        self.ai_control = False                   # set by the sidebar toggle; gates the MCP control tools
        self.mcp = None                           # the running MCP server handle (crank.mcp_server)
        self.traffic = TrafficLog(logs_dir or Path(store.root).parent / "logs", session=self._log_session)
        self.connection = ObdConnection(log=self.traffic, heartbeat_s=heartbeat_s, read_lock=self._obd_lock)

    def sync(self, **fields) -> None:
        """The laptop UI pushes its current choices here on every run."""
        for k, v in fields.items():
            if k in SYNCABLE and v is not None:
                setattr(self, k, v)

    @property
    def running(self) -> bool:
        return bool(self.job and self.job.running)

    def arm(self, label: str = "", note: str = "") -> str | None:
        """Start a capture. Returns a message if it could not start, else None."""
        with self._lock:
            if self.running:
                return "A capture is already running."
            if self.mode == "Simulator":
                scope = SimScope(self.scenario, fault=self.sim_fault)
            elif self.scope is not None:
                scope = self.scope
            else:
                return "Connect to the scope first (on the laptop)."
            sess = self.store.open(self.session_name)
            case = Case(sess)
            self.job = CaptureJob(
                scope, self.settings, self.thresholds, poll_s=self.poll_s,
                save=lambda d: sess.add_capture(d, label, note),
                on_done=lambda j: case.append_capture(j.saved_id, label, j.analysis,
                                                      bool(j.data and j.data.meta.get("sim"))))
            self.job.start()
            return None

    def case(self) -> Case:
        return Case(self.store.open(self.session_name))

    def _obd_transport(self, source):
        if isinstance(source, str):
            return SimTransport(source)
        kind, value = source
        if kind == "serial":
            return SerialTransport(resolve_port(value))
        raise ObdError(f"Unknown OBD source {kind!r}.")

    def _wrong_kind(self, transport) -> str:
        """'' if this read may go into the current case, else why not. A case holds real OBD reads or
        simulated ones, never both, so a practice run cannot be mistaken for evidence."""
        kind = "simulated" if transport.sim else "real"
        others = self.case().obd_kinds() - {kind}
        if not others:
            return ""
        have = sorted(others)[0]
        use = "the simulator" if have == "real" else "a real adapter"
        return f"This case already has {have} OBD reads. Start a new case to use {use}."

    def delete_session(self, name: str) -> str | None:
        """Delete a session. Returns a message if it could not be deleted, else None.
        Moves off it first when it is the current one, so the traffic log does not recreate its folder."""
        with self._lock:
            if self.running and name == self.session_name:
                return "A capture is running on this session; wait for it to finish."
            job = self.obd_job
            if job and job.running and job.session_name == name:
                return "An OBD read is running on this session; wait for it or cancel it first."
            if name == self.session_name:
                self.session_name = next((n for n in self.store.list_sessions() if n != name), "default")
            try:
                self.store.delete_session(name)
            except KeyError:
                return f"There is no session '{name}'."
            self.store.open(self.session_name)                # never leave the app with no session
            return None

    def _log_session(self):
        return self.session_name, self.store.open(self.session_name).path

    def _take_obd_lock(self) -> bool:
        """The read lock, waiting briefly if only the heartbeat has it (a ping lasts a moment)."""
        if self._obd_lock.acquire(blocking=False):
            return True
        if self.connection.pinging:
            return self._obd_lock.acquire(timeout=3.0)
        return False

    DROPPED = ("The adapter connection dropped. Press Relink, or Disconnect to read without a standing "
               "connection.")

    def start_obd(self, source="healthy") -> str | None:
        """Start one OBD read into the current case in the background. Returns a message if it could not
        start, else None. `source` is a simulator scenario name, or ("serial", port | "auto") for a real adapter;
        it is ignored while a standing connection is up.

        The lock is held until the job ends, so a second read from the phone or another tab is refused."""
        if not self._take_obd_lock():
            return "An OBD read is already running."
        started = False
        try:
            conn = self.connection
            held = conn if conn.state == "connected" else None
            if conn.state == "dropped":
                return self.DROPPED
            if held:
                transport = HeldTransport(conn.transport)
            else:
                if source == "connection":
                    return "The connection is closed. Press Connect, or pick an adapter."
                try:
                    transport = LoggingTransport(self._obd_transport(source), self.traffic)
                except ObdError as e:
                    return str(e)
            wrong = self._wrong_kind(transport)
            if wrong:
                return wrong
            case = self.case()                              # the read goes to the case it was started on
            adapter = held.adapter if held and not held.cold else None

            def run(cancel, stop):
                self.traffic.event(f"read started ({'open connection' if held else 'own port'}, "
                                   f"{'warm' if adapter else 'full setup'})")
                try:
                    readout = ObdReader(transport, cancel=cancel, stop=stop, adapter=adapter).read()
                except ObdCancelled:
                    self.traffic.event("read cancelled")
                    if held:
                        held.mark_cold()
                    raise
                except ObdError as e:
                    self.traffic.event(f"read failed: {e}")
                    if held:
                        held.mark_cold()
                        if isinstance(e, ObdVehicleSilent) or (
                                isinstance(e, ObdAdapterError) and e.kind in ("UNABLE TO CONNECT", "BUS INIT")):
                            held.note_vehicle(False)
                    raise
                if held:
                    held.adopt(readout)
                case.append_obd(readout)                    # last step: a cancelled or failed read adds nothing
                self.traffic.event("read stopped" if readout.stopped else "read finished")
                return readout
            job = ObdJob(run, session_name=self.session_name, on_finish=self._obd_lock.release)
            self.obd_job = job
            job.start()
            started = True
            return None
        finally:
            if not started:
                self._obd_lock.release()

    def cancel_obd(self) -> None:
        """Throw the running read away; nothing is recorded."""
        job = self.obd_job
        if job and job.running:
            job.cancel()

    def stop_obd(self) -> None:
        """End the running read and keep what was read (acts as a cancel before the PCM has answered)."""
        job = self.obd_job
        if job and job.running:
            job.stop()

    def read_obd(self, source="healthy"):
        """One OBD read into the current case, waiting for it to finish.
        Returns (readout, None) or (None, message)."""
        err = self.start_obd(source)
        if err:
            return None, err
        job = self.obd_job
        job.join()
        return (job.readout, None) if job.state in ("done", "stopped") else (None, job.message)

    def connect(self, source) -> str | None:
        """Hold the adapter open. Returns a message if it could not."""
        if not self._take_obd_lock():
            return "A read is running; wait for it or cancel it first."
        try:
            if self.connection.state == "connected":
                return "Already connected."
            try:
                transport = self._obd_transport(source)
            except ObdError as e:
                return str(e)
            wrong = self._wrong_kind(transport)
            if wrong:
                return wrong
            return self.connection.connect(transport, kind="simulated" if transport.sim else "real",
                                           source=source)
        finally:
            self._obd_lock.release()

    def disconnect(self) -> None:
        if not self._take_obd_lock():
            return
        try:
            self.connection.disconnect()
        finally:
            self._obd_lock.release()

    def relink(self) -> str | None:
        """Close the connection and open it again with the same source."""
        source = self.connection.source
        if not source:
            return "Nothing to relink. Press Connect."
        self.disconnect()
        if self.connection.state not in ("disconnected",):
            return "A read is running; wait for it or cancel it first."
        return self.connect(source)

    def connection_status(self) -> dict:
        s = self.connection.status()
        s["reading"] = bool(self.obd_job and self.obd_job.running)
        s["traffic_error"] = self.traffic.last_error
        return s

    def cancel(self) -> None:
        if self.job:
            self.job.cancel()

    def snapshot(self) -> dict:
        job = self.job
        snap = dict(mode=self.mode, scenario=self.scenario, session_name=self.session_name,
                    state=job.state if job else "idle", message=job.message if job else "",
                    running=self.running, seconds_left=job.seconds_left if job else 0.0,
                    saved_id=job.saved_id if job else None, hook_error=job.hook_error if job else "",
                    findings=[], warnings=[])
        if job and job.analysis:
            snap["findings"] = [{"severity": f.severity, "title": f.title}
                                for f in sorted(job.analysis.findings,
                                                key=lambda f: _SEVERITY_ORDER[f.severity])]
        if job:
            snap["warnings"] = [m for m in getattr(job.scope, "log", []) if m.startswith("WARNING")]
        return snap


def lan_ip() -> str:
    """The address a phone on the same network would use (no packets are actually sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "localhost"


_controller: Controller | None = None
_guard = threading.Lock()


def get_controller() -> Controller:
    global _controller
    with _guard:
        if _controller is None:
            _controller = Controller(SessionStore(os.environ.get("CRANK_SESSIONS_DIR", "sessions")),
                                     float(os.environ.get("CRANK_POLL_S", "0.2")),
                                     float(os.environ.get("CRANK_HEARTBEAT_S", "5")),
                                     os.environ.get("CRANK_LOGS_DIR") or None)
        return _controller


def reset_controller() -> None:
    global _controller
    with _guard:
        if _controller is not None and _controller.mcp is not None:
            from .mcp_server import stop_mcp_server           # imported here: mcp_server never imports this module
            stop_mcp_server(_controller)
        _controller = None
