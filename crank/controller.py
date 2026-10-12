"""Shared server-side state so a phone session and the laptop session drive the same capture."""
from __future__ import annotations

import os
import socket
import threading

from .capture import CaptureJob
from .case import Case
from .model import CaptureSettings, Thresholds
from .obd.errors import ObdError
from .obd.reader import ObdReader
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

    def read_obd(self, scenario: str = "healthy"):
        """One OBD read into the current case. Returns (readout, None) or (None, message).

        Only the simulator is wired in; a real adapter plugs in as another Transport."""
        try:
            readout = ObdReader(SimTransport(scenario)).read()
        except ObdError as e:
            return None, str(e)
        self.case().append_obd(readout)
        return readout, None

    def cancel(self) -> None:
        if self.job:
            self.job.cancel()

    def snapshot(self) -> dict:
        job = self.job
        snap = dict(mode=self.mode, scenario=self.scenario, session_name=self.session_name,
                    state=job.state if job else "idle", message=job.message if job else "",
                    running=self.running, seconds_left=job.seconds_left if job else 0.0,
                    saved_id=job.saved_id if job else None, findings=[], warnings=[])
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
