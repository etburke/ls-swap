"""Serial transport for classic-Bluetooth ELM327 / STN adapters (e.g. OBDLink MX+, which appears as a
macOS serial port). Every command goes through the read-only allowlist before it touches the port."""
from __future__ import annotations

import fnmatch
import threading
import time

from . import allowlist
from .errors import ObdCancelled, ObdNoAdapter, ObdTimeout
from .transport import StreamEnd, Transport

BAUD_FALLBACK = 38400


def _default_factory(port: str, baud: int, timeout: float):
    import serial                                     # lazy: only needed with a real adapter
    return serial.Serial(port, baud, timeout=timeout)


class SerialTransport(Transport):
    sim = False

    def __init__(self, port: str, baud: int = 115200, serial_factory=None, atz_timeout: float = 3.0):
        self.port, self.baud, self.atz_timeout = port, baud, atz_timeout
        self._factory = serial_factory or _default_factory
        self._ser = None
        self._lock = threading.Lock()

    def _connect(self, baud: int) -> None:
        try:
            self._ser = self._factory(self.port, baud, 0.1)
        except (OSError, ValueError, ImportError) as e:
            raise ObdNoAdapter(f"Could not open {self.port}: {e}. Is the adapter paired and not in use "
                               "by another app?") from e

    def _atz(self) -> None:
        """ATZ, once more if it gets no prompt: a sleeping MX+ wakes on the connection and can lose the first command."""
        try:
            self.send("ATZ", self.atz_timeout)
        except ObdTimeout:
            self.send("ATZ", self.atz_timeout)

    def open(self) -> None:
        bauds = [self.baud] + ([BAUD_FALLBACK] if self.baud != BAUD_FALLBACK else [])
        for i, baud in enumerate(bauds):
            self._connect(baud)
            try:
                self._atz()
                self.baud = baud
                return
            except ObdCancelled:
                self.close()
                raise
            except ObdTimeout:
                self.close()
                if i == len(bauds) - 1:
                    raise

    def send(self, cmd: str, timeout: float = 5.0) -> str:
        norm = allowlist.check(cmd)                   # refuse before anything is written
        if self._ser is None:
            raise ObdNoAdapter("The adapter is not open.")
        self.check_cancel()
        with self._lock:
            try:
                self._ser.reset_input_buffer()
                self._ser.write(norm.encode() + b"\r")
                buf, deadline = b"", time.monotonic() + timeout
                while b">" not in buf:
                    self.check_cancel()
                    if time.monotonic() > deadline:
                        raise ObdTimeout("The adapter did not answer. Check it is plugged in and powered.")
                    buf += self._ser.read(max(1, self._ser.in_waiting or 0))
            except OSError as e:
                raise ObdNoAdapter(f"The adapter connection dropped: {e}.") from e
        return buf.split(b">", 1)[0].decode("ascii", errors="replace")

    def stream(self, cmd, on_line, stop, max_seconds, silence_s=1.0) -> StreamEnd:
        """Run a monitor command and hand each line to `on_line`. Ends on stop, time, silence, or when the
        adapter itself ends it; our own endings stop the monitor with one space (never a bare CR, which
        would repeat the command) and resync."""
        norm = allowlist.check(cmd)
        if self._ser is None:
            raise ObdNoAdapter("The adapter is not open.")
        self.check_cancel()
        ours = ("stopped", "time", "silent")
        with self._lock:
            try:
                try:
                    end = self._read_stream(norm, on_line, stop, max_seconds, silence_s)
                except BaseException:                     # cancel, a bad on_line, Ctrl-C: never leave the adapter monitoring
                    try:
                        self._stop_monitor()
                    except OSError:
                        pass                              # a dropped link: let the original exception surface
                    raise
                if end.reason in ours:
                    self._stop_monitor()
            except OSError as e:
                raise ObdNoAdapter(f"The adapter connection dropped: {e}.") from e
        if end.reason in ours:
            self._resync()
        return end

    def _read_stream(self, norm, on_line, stop, max_seconds, silence_s) -> StreamEnd:
        self._ser.reset_input_buffer()
        self._ser.write(norm.encode() + b"\r")
        buf, start = b"", time.monotonic()
        last_line = start
        while True:
            tail = buf.decode("ascii", errors="replace").strip()
            if stop.is_set():
                return StreamEnd("stopped", tail)
            self.check_cancel()
            now = time.monotonic()
            if now - start >= max_seconds:
                return StreamEnd("time", tail)
            if now - last_line >= silence_s:
                return StreamEnd("silent", tail)
            buf += self._ser.read(max(1, self._ser.in_waiting or 0))
            *lines, buf = buf.split(b"\r")
            for raw in lines:
                line = raw.decode("ascii", errors="replace").strip()
                if not line or line == norm:
                    continue
                if "BUFFER FULL" in line:
                    return StreamEnd("buffer_full")
                if "LV RESET" in line or line.startswith(("ELM327", "STN")):
                    return StreamEnd("reset")
                if line == ">":
                    return StreamEnd("prompt")
                last_line = time.monotonic()
                on_line(time.time(), line)
                if stop.is_set():
                    return StreamEnd("stopped")
            if buf.strip() == b">":
                return StreamEnd("prompt")

    def _stop_monitor(self, timeout: float = 2.0) -> None:
        """One space stops monitoring; wait for the prompt. Caller holds the lock."""
        self._ser.write(b" ")
        buf, deadline = b"", time.monotonic() + timeout
        while b">" not in buf and time.monotonic() < deadline:
            buf += self._ser.read(max(1, self._ser.in_waiting or 0))

    def _resync(self) -> None:
        """Confirm the adapter is back to normal: ATI answers with ELM327 (a stray space can draw one '?')."""
        reply = self.send("ATI", 2.0)
        if "?" in reply:
            reply = self.send("ATI", 2.0)
        if "ELM327" not in reply:
            raise ObdTimeout("The adapter did not come back after monitoring.")

    def close(self) -> None:
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except OSError:
                pass


ADAPTER_GLOBS = ("/dev/cu.OBDLink*", "/dev/cu.*OBD*", "/dev/cu.usbserial*")


def _comports():
    from serial.tools import list_ports as lp          # lazy, like the transport
    return lp.comports()


def list_ports(comports=None) -> list[tuple[str, str]]:
    """(label, path) for serial ports that look like OBD adapters."""
    out = []
    for p in (comports or _comports)():
        if any(fnmatch.fnmatch(p.device, g) for g in ADAPTER_GLOBS):
            out.append((f"{p.device} ({p.description})", p.device))
    return out


def resolve_port(port: str, comports=None) -> str:
    """'auto' picks the only adapter-looking port; anything else is taken as a path."""
    if port != "auto":
        return port
    found = [path for _, path in list_ports(comports)]
    if len(found) == 1:
        return found[0]
    if not found:
        raise ObdNoAdapter("No OBD adapter port found. Make sure the adapter is paired in Bluetooth settings, then refresh.")
    raise ObdNoAdapter("More than one possible adapter port: " + ", ".join(found) + ". Choose one.")
