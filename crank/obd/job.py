"""ObdJob: one OBD read in a background thread, so the UI can show it running, stop it or cancel it."""
from __future__ import annotations

import threading
import time
from typing import Callable

from .errors import ObdCancelled, ObdError


class ObdJob:
    def __init__(self, run: Callable[[threading.Event, threading.Event], object], session_name: str = "",
                 on_finish: Callable[[], None] | None = None):
        self._run, self.session_name, self._on_finish = run, session_name, on_finish
        self.cancel_event, self.stop_event = threading.Event(), threading.Event()
        self.state, self.message, self.readout = "idle", "", None
        self.started_at = self.finished_at = 0.0
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.started_at = time.time()
        self.state = "running"
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        """Throw the read away: nothing is recorded."""
        self.cancel_event.set()

    def stop(self) -> None:
        """End the read and keep what was read (before the PCM has answered, this acts as cancel)."""
        self.stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def seconds(self) -> float:
        if not self.started_at:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    def _work(self) -> None:
        state, message, readout = "error", "", None
        try:
            readout = self._run(self.cancel_event, self.stop_event)
            state = "stopped" if getattr(readout, "stopped", False) else "done"
        except ObdCancelled as e:
            state, message = "cancelled", str(e)
        except ObdError as e:
            message = str(e)
        except Exception as e:                        # a storage or parsing bug: nothing was recorded
            message = f"{type(e).__name__}: {e}"
        finally:
            if self._on_finish:
                self._on_finish()                     # free the controller's lock before anyone sees the end
            self.readout, self.message, self.finished_at = readout, message, time.time()
            self.state = state
