"""ObdReader: run one full read (codes, freeze frame, live snapshot) over a Transport."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import protocol as P
from .errors import ObdNotSupported, ObdProtocolError
from .pids import FREEZE_PIDS, LIVE_PIDS, PIDS
from .transport import Transport

INIT = ("ATZ", "ATE0", "ATL0", "ATS0", "ATH0", "ATSP0")
STATUS_MODES = (("stored", 0x03), ("pending", 0x07), ("permanent", 0x0A))


@dataclass
class LiveValue:
    pid: int
    name: str
    unit: str
    value: float | None          # None = the PCM does not support / did not answer this PID

    @property
    def supported(self) -> bool:
        return self.value is not None


@dataclass
class ObdReadout:
    codes: list = field(default_factory=list)            # [{"code","status","description"}]
    freeze_frame: dict | None = None                     # {"code","description","values":[LiveValue-like dicts]}
    live: list = field(default_factory=list)             # [LiveValue]
    mil_on: bool | None = None
    protocol: str = ""
    unsupported_modes: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    sim: bool = False
    read_at: str = ""

    @property
    def dtc_count(self) -> int:
        return len(self.codes)

    def lines(self) -> list[str]:
        """Plain-language result lines for the UI."""
        sim = " (simulated)" if self.sim else ""
        codes = ", ".join(f"{c['code']} ({c['status']})" for c in self.codes)
        if not codes:
            codes = ("no codes reported by the modes that were read" if self.unsupported_modes
                     else "none stored, pending or permanent")
        out = [f"Codes{sim}: " + codes]
        if self.mil_on is not None:
            out.append("Check engine light: " + ("on" if self.mil_on else "off"))
        got = [v for v in self.live if v.supported]
        out.append(f"Live values read: {len(got)} of {len(self.live)}"
                   + (f" (not supported: {', '.join(v.name for v in self.live if not v.supported)})"
                      if len(got) < len(self.live) else ""))
        if self.freeze_frame:
            out.append(f"Freeze frame for {self.freeze_frame['code']}")
        if self.unsupported_modes:
            out.append("PCM does not support mode(s): " + ", ".join(self.unsupported_modes))
        return out

    def to_event_data(self) -> dict:
        return {"codes": self.codes, "freeze_frame": self.freeze_frame, "mil_on": self.mil_on,
                "source": "obd", "sim": self.sim, "protocol": self.protocol,
                "unsupported_modes": self.unsupported_modes, "raw": self.raw}


class ObdReader:
    def __init__(self, transport: Transport, timeout: float = 5.0):
        self.t, self.timeout = transport, timeout
        self.raw: dict = {}

    def _ask(self, cmd: str) -> str:
        resp = self.t.send(cmd, self.timeout)
        self.raw[cmd] = resp.strip()
        return resp

    def _mode_data(self, mode: int, pid: int | None = None, frame: bool = False) -> bytes | None:
        return P.response_data(self._ask(P.request(mode, pid, frame)), mode, pid, frame)

    def read(self) -> ObdReadout:
        self.raw = {}
        out = ObdReadout(sim=self.t.sim, read_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        self.t.open()
        try:
            for cmd in INIT:
                self._ask(cmd)
            first = self._mode_data(0x01, 0x00)           # triggers protocol search
            if first is None:
                raise ObdProtocolError("The vehicle did not answer. Is the key on?")
            out.protocol = self._ask("ATDPN").strip()
            can = P.is_can(out.protocol)
            supported = P.supported_pids(0x00, first)
            for base in (0x20, 0x40):
                if base in supported:
                    data = self._mode_data(0x01, base)
                    supported |= P.supported_pids(base, data) if data else set()
            self._status(out)
            self._codes(out, can)
            self._freeze(out)
            self._live(out, supported)
        except BaseException:
            try:
                self.t.close()
            except Exception:                              # never mask the error that got us here
                pass
            raise
        self.t.close()
        out.raw = self.raw
        return out

    def _status(self, out: ObdReadout) -> None:
        data = self._mode_data(0x01, 0x01)
        out.mil_on = bool(data[0] & 0x80) if data else None

    def _codes(self, out: ObdReadout, can: bool) -> None:
        for status, mode in STATUS_MODES:
            try:
                data = self._mode_data(mode)
            except ObdNotSupported:
                data = None
                out.unsupported_modes.append(f"{mode:02X}")
            for code in P.decode_dtcs(data or b"", can):
                out.codes.append({"code": code, "status": status, "description": P.describe_dtc(code) or ""})

    def _freeze(self, out: ObdReadout) -> None:
        try:
            data = self._mode_data(0x02, 0x02, frame=True)
        except ObdNotSupported:
            out.unsupported_modes.append("02")
            return
        if not data or len(data) < 2:
            return
        if data[0] == 0 and data[1] == 0:                  # 42 02 00 00: the PCM holds no freeze frame
            return
        code = P.decode_dtc(data[0], data[1])
        values = []
        for pid in FREEZE_PIDS:
            try:
                d = self._mode_data(0x02, pid, frame=True)
            except ObdNotSupported:
                d = None
            dec = P.decode_pid(pid, d) if d else None
            if dec:
                values.append({"name": PIDS[pid].name, "value": dec[0], "unit": dec[1]})
        out.freeze_frame = {"code": code, "description": P.describe_dtc(code) or "", "values": values}

    def _live(self, out: ObdReadout, supported: set) -> None:
        for pid in LIVE_PIDS:
            p, value = PIDS[pid], None
            if pid in supported:
                try:
                    d = self._mode_data(0x01, pid)
                except ObdNotSupported:
                    d = None
                dec = P.decode_pid(pid, d) if d else None
                value = dec[0] if dec else None
            out.live.append(LiveValue(pid, p.name, p.unit, value))
