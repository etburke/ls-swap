"""CaptureJob: configure, arm, wait for trigger, download, save, analyze, in a thread."""
from __future__ import annotations

import threading
import time
from typing import Callable

from .analysis import Analysis, analyze_capture
from .model import CaptureData, CaptureSettings, Thresholds
from .scope import ScopeError, ScopeInterface

STATES = ("idle", "configuring", "armed", "triggered", "downloading",
          "analyzing", "done", "error", "cancelled")
ACTIVE = ("configuring", "armed", "triggered", "downloading", "analyzing")


class CaptureJob:
    def __init__(self, scope: ScopeInterface, settings: CaptureSettings,
                 thresholds: Thresholds | None = None,
                 save: Callable[[CaptureData], object] | None = None,
                 poll_s: float = 0.2, never_armed_s: float = 3.0,
                 on_done: Callable[["CaptureJob"], None] | None = None):
        self.scope, self.settings = scope, settings
        self.thresholds = thresholds or Thresholds()
        self.save, self.poll_s, self.never_armed_s = save, poll_s, never_armed_s
        self.on_done, self.hook_error, self._notified = on_done, "", False
        self.state, self.message = "idle", ""
        self.data: CaptureData | None = None
        self.analysis: Analysis | None = None
        self.saved_id = None
        self.started_at = 0.0
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    # -- control
    def start(self) -> None:
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self.state in ACTIVE

    @property
    def seconds_left(self) -> float:
        return max(0.0, self.settings.wait - (time.time() - self.started_at))

    # -- worker
    def _set(self, state: str, message: str = "") -> None:
        self.state, self.message = state, message

    def _run(self) -> None:
        try:
            self._set("configuring")
            self.scope.configure(self.settings)
            self.scope.arm()
            self._set("armed", "Crank the engine now.")
            if not self._wait_for_trigger():
                return
            self._set("downloading")
            self.data = self.scope.fetch(self.settings)
            if self.save:
                self.saved_id = self.save(self.data)  # on disk before analysis can fail
            self._set("analyzing")
            self.analysis = analyze_capture(self.data, self.thresholds)
            self._notify()
            self._set("done")
        except ScopeError as e:
            self._set("error", str(e))
        except Exception as e:                        # analysis or storage bug: data is kept
            self._set("error", f"{type(e).__name__}: {e}")
        finally:
            self._notify()                            # a saved capture always reaches the case log

    def _notify(self) -> None:
        if self.on_done and self.saved_id is not None and not self._notified:
            self._notified = True
            try:
                self.on_done(self)
            except Exception as e:                    # logging must never break a capture
                self.hook_error = f"{type(e).__name__}: {e}"

    def _wait_for_trigger(self) -> bool:
        t0, seen_armed, t_trig = time.time(), False, None
        while True:
            if self._cancel.is_set():
                self.scope.disarm()
                self._set("cancelled", "Capture cancelled; scope disarmed.")
                return False
            st = self.scope.status()
            if st != "STOP":
                seen_armed = True
                if st == "TD":
                    t_trig = t_trig or time.time()
                    self._set("triggered", "Trigger seen; recording.")
            elif seen_armed:
                return True
            elif time.time() - t0 > self.never_armed_s:
                self._set("error", "The scope never armed after SINGLE (status stayed STOP). "
                                   "Check the SCPI log for rejected commands.")
                return False
            if t_trig and time.time() - t_trig > self.settings.seconds + 10:
                self.scope.disarm()
                self._set("error", "The scope triggered but never finished recording.")
                return False
            if t_trig is None and time.time() - t0 > self.settings.wait:
                self.scope.disarm()
                self._set("error", "Timed out waiting for a trigger. Is CH1 crossing the "
                                   "trigger level while cranking?")
                return False
            time.sleep(self.poll_s)
