"""Shared server-side state so a phone session and the laptop session drive the same capture."""
from __future__ import annotations

import os
import socket
import threading

from .capture import CaptureJob
from .case import Case
from .model import CaptureSettings, Thresholds
from .obd.errors import ObdError
from .obd.job import ObdJob
from .obd.reader import ObdReader
from .obd.serial_transport import SerialTransport, resolve_port
from .obd.transport import SimTransport
from .scope import RigolScope, SimScope
from .store import SessionStore

SYNCABLE = ("mode", "scenario", "session_name", "settings", "thresholds")
_SEVERITY_ORDER = {"fail": 0, "warn": 1, "info": 2}


class Controller:
    def __init__(self, store: SessionStore, poll_s: float = 0.2):
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

    def start_obd(self, source="healthy") -> str | None:
        """Start one OBD read into the current case in the background. Returns a message if it could not
        start, else None. `source` is a simulator scenario name, or ("serial", port | "auto") for a real adapter.

        The lock is held until the job ends, so a second read from the phone or another tab is refused."""
        if not self._obd_lock.acquire(blocking=False):
            return "An OBD read is already running."
        started = False
        try:
            try:
                transport = self._obd_transport(source)
            except ObdError as e:
                return str(e)
            wrong = self._wrong_kind(transport)
            if wrong:
                return wrong
            case = self.case()                              # the read goes to the case it was started on

            def run(cancel, stop):
                readout = ObdReader(transport, cancel=cancel, stop=stop).read()
                case.append_obd(readout)                    # last step: a cancelled or failed read adds nothing
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
                                     float(os.environ.get("CRANK_POLL_S", "0.2")))
        return _controller


def reset_controller() -> None:
    global _controller
    with _guard:
        _controller = None
