"""Serial transport for classic-Bluetooth ELM327 / STN adapters (e.g. OBDLink MX+, which appears as a
macOS serial port). Every command goes through the read-only allowlist before it touches the port."""
from __future__ import annotations

import fnmatch
import threading
import time

from . import allowlist
from .errors import ObdNoAdapter, ObdTimeout
from .transport import Transport

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

    def open(self) -> None:
        bauds = [self.baud] + ([BAUD_FALLBACK] if self.baud != BAUD_FALLBACK else [])
        for i, baud in enumerate(bauds):
            self._connect(baud)
            try:
                self.send("ATZ", self.atz_timeout)
                self.baud = baud
                return
            except ObdTimeout:
                self.close()
                if i == len(bauds) - 1:
                    raise

    def send(self, cmd: str, timeout: float = 5.0) -> str:
        norm = allowlist.check(cmd)                   # refuse before anything is written
        if self._ser is None:
            raise ObdNoAdapter("The adapter is not open.")
        with self._lock:
            try:
                self._ser.reset_input_buffer()
                self._ser.write(norm.encode() + b"\r")
                buf, deadline = b"", time.monotonic() + timeout
                while b">" not in buf:
                    if time.monotonic() > deadline:
                        raise ObdTimeout("The adapter did not answer. Check it is plugged in and powered.")
                    buf += self._ser.read(max(1, self._ser.in_waiting or 0))
            except OSError as e:
                raise ObdNoAdapter(f"The adapter connection dropped: {e}.") from e
        return buf.split(b">", 1)[0].decode("ascii", errors="replace")

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
