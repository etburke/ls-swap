"""ObdConnection: the adapter port held open between reads, with a heartbeat so a drop is noticed."""
from __future__ import annotations

import threading
import time

from .adapter import AdapterInfo, parse_voltage
from .errors import ObdError, ObdNoAdapter
from .reader import ObdReader
from .trafficlog import LoggingTransport, TrafficLog

CAN_CONNECT = ("disconnected", "dropped", "error")


class ObdConnection:
    def __init__(self, log: TrafficLog | None = None, heartbeat_s: float = 5.0, misses: int = 2,
                 read_lock: threading.Lock | None = None, now=time.time):
        self.log, self.heartbeat_s, self.misses = log, heartbeat_s, misses
        self.read_lock, self._now = read_lock, now
        self.state, self.message = "disconnected", ""
        self.adapter: AdapterInfo | None = None
        self.voltage = self.voltage_at = self.last_heard = None
        self.vehicle, self.vehicle_at = "not checked", None
        self.cold, self.kind, self.source = False, "", ""
        self.pinging = False
        self._inner = None
        self.transport = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._misses = 0

    # -- log helpers
    def _event(self, text: str, tag: str = "") -> None:
        if self.log:
            self.log.event(text, tag)

    def _tag(self) -> str:
        return "sim" if self.kind == "simulated" else ""

    # -- control
    def connect(self, transport, kind: str = "real", source="") -> str | None:
        """Open the port, run the setup commands, identify the adapter, read its voltage and start the
        heartbeat. Never sends an OBD request. Returns a message on failure (state 'error')."""
        if self.state not in CAN_CONNECT:
            return "Already connected." if self.state == "connected" else "Already connecting."
        self.state, self.message, self.kind, self.source = "connecting", "", kind, source
        self._event("connecting", self._tag())
        wrapped = LoggingTransport(transport, self.log) if self.log else transport
        try:
            wrapped.open()
            adapter = ObdReader(wrapped).prepare()
            self.voltage = parse_voltage(wrapped.send("ATRV", 2.0))
        except ObdError as e:
            self._close(wrapped)
            self.state, self.message, self.transport, self._inner = "error", str(e), None, None
            self._event(f"connect failed: {e}", self._tag())
            return self.message
        self._inner, self.transport, self.adapter = transport, wrapped, adapter
        self.voltage_at = self.last_heard = self._now()
        self.vehicle, self.vehicle_at, self.cold, self._misses = "not checked", None, False, 0
        self.state = "connected"
        self._event(f"connected: {adapter.label()}", self._tag())
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, args=(self._stop,), daemon=True)
        self._thread.start()
        return None

    def disconnect(self) -> None:
        was = self.state
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(3.0)
        self._thread = None
        self._close(self.transport)
        self.transport = self._inner = None
        if was != "disconnected":
            self.state, self.message = "disconnected", ""
            self._event("disconnected", self._tag())

    @staticmethod
    def _close(t) -> None:
        if t is not None:
            try:
                t.close()
            except Exception:                         # closing must never raise
                pass

    # -- heartbeat
    def _beat(self, stop: threading.Event) -> None:
        while not stop.wait(self.heartbeat_s):
            if self.state != "connected":
                continue
            if self.read_lock is not None and not self.read_lock.acquire(blocking=False):
                continue
            self.pinging = True
            try:
                self._ping()
            finally:
                self.pinging = False
                if self.read_lock is not None:
                    self.read_lock.release()

    def _ping(self) -> None:
        tag = "hb"
        if self.log:
            self.log.command("ATRV", tag)
        start = time.monotonic()
        try:
            reply = self._inner.send("ATRV", 2.0)
        except ObdNoAdapter as e:
            self._log_error(e)
            self._drop(f"heartbeat failed: {e}")
            return
        except ObdError as e:
            self._log_error(e)
            self._miss(f"heartbeat got no answer ({e})")
            return
        if self.log:
            self.log.reply(reply, (time.monotonic() - start) * 1000, tag)
        v = parse_voltage(reply)
        if v is None:
            if "RESET" in reply.upper():
                self.cold = True                      # the adapter forgot its settings
            self._miss(f"heartbeat got {reply.strip() or 'nothing'!r} instead of a voltage")
            return
        self.voltage, self.voltage_at, self.last_heard, self._misses = v, self._now(), self._now(), 0

    def _log_error(self, e: Exception) -> None:
        if self.log:
            self.log.error(e, "hb")

    def _miss(self, why: str) -> None:
        self._misses += 1
        self._event(f"heartbeat miss {self._misses}: {why}", "hb")
        if self._misses >= self.misses:
            self._drop(why)

    def _drop(self, why: str) -> None:
        self._stop.set()
        self._close(self.transport)
        self.transport = self._inner = None
        self.state, self.message = "dropped", why
        self._event(f"dropped: {why}", self._tag())

    # -- facts the controller feeds in
    def note_vehicle(self, answered: bool) -> None:
        self.vehicle, self.vehicle_at = ("answered" if answered else "no answer"), self._now()

    def mark_cold(self) -> None:
        self.cold = True

    def adopt(self, readout) -> None:
        """A read on this connection finished: keep the adapter it identified and the vehicle's answer."""
        self.adapter = readout.adapter or self.adapter
        self.cold = False
        self.note_vehicle(True)

    def status(self) -> dict:
        now = self._now()

        def age(t):
            return None if t is None else max(0.0, now - t)
        return {"state": self.state, "message": self.message,
                "adapter": self.adapter.label() if self.adapter and self.state == "connected" else "",
                "voltage": self.voltage, "voltage_age": age(self.voltage_at),
                "last_heard_age": age(self.last_heard), "vehicle": self.vehicle,
                "vehicle_age": age(self.vehicle_at), "kind": self.kind, "source": self.source,
                "cold": self.cold}
