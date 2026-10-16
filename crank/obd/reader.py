"""ObdReader: run one full read (codes, freeze frame, live snapshot) over a Transport."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import protocol as P
from .errors import ObdAdapterError, ObdNotSupported, ObdProtocolError
from .pids import FREEZE_PIDS, LIVE_PIDS, PIDS
from .transport import Transport

INIT = ("ATZ", "ATE0", "ATL0", "ATS0", "ATH1", "ATSP0")
STATUS_MODES = (("stored", 0x03), ("pending", 0x07), ("permanent", 0x0A))
MAX_RETRIES = 3


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
    ecus: list = field(default_factory=list)             # every ECU header that answered, e.g. ["7E8", "7E9"]
    primary_ecu: str = ""                                # the PCM: live values, freeze frame, supported PIDs
    notes: list = field(default_factory=list)
    sim: bool = False
    read_at: str = ""

    @property
    def dtc_count(self) -> int:
        return len(self.codes)

    def lines(self) -> list[str]:
        """Plain-language result lines for the UI."""
        sim = " (simulated)" if self.sim else ""
        multi = len(self.ecus) > 1
        codes = ", ".join(f"{c['code']} ({c['status']}" + (f", {c['ecu']}" if multi and c.get("ecu") else "") + ")"
                          for c in self.codes)
        if not codes:
            codes = ("no codes reported by the modes that were read" if self.unsupported_modes
                     else "none stored, pending or permanent")
        out = [f"Codes{sim}: " + codes]
        if multi:
            out.append("ECUs that answered: " + ", ".join(self.ecus))
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
        return out + list(self.notes)

    def to_event_data(self) -> dict:
        return {"codes": self.codes, "freeze_frame": self.freeze_frame, "mil_on": self.mil_on,
                "source": "obd", "sim": self.sim, "protocol": self.protocol,
                "unsupported_modes": self.unsupported_modes, "raw": self.raw,
                "ecus": self.ecus, "primary_ecu": self.primary_ecu, "notes": self.notes}


class ObdReader:
    def __init__(self, transport: Transport, timeout: float = 5.0, first_timeout: float = 20.0,
                 retry_pause: float = 0.3):
        self.t, self.timeout, self.first_timeout, self.retry_pause = transport, timeout, first_timeout, retry_pause
        self.raw: dict = {}
        self.primary = ""
        self.notes: list = []
        self._retries = 0

    def _ask(self, cmd: str, timeout: float | None = None) -> str:
        resp = self.t.send(cmd, timeout or self.timeout)
        self.raw[cmd] = resp.strip()
        return resp

    def _request(self, cmd: str, parse, timeout: float | None = None):
        """Send an OBD request and parse the reply. A transient adapter error is retried once; LV RESET
        re-runs INIT first. Each retry is noted. Returns (reply text, parse(reply))."""
        retried = False
        while True:
            try:
                resp = self._ask(cmd, timeout)
                return resp, parse(resp)
            except ObdAdapterError as e:
                if retried or self._retries >= MAX_RETRIES or not (e.transient or e.reset):
                    raise
                retried = True
                self._retries += 1
                if e.reset:
                    self.notes.append(f"The adapter reset ({e.kind}) during {cmd}; its settings were "
                                      "re-applied and the request retried.")
                    for init in INIT:
                        self._ask(init)
                    timeout = self.first_timeout                  # the protocol search starts again
                else:
                    self.notes.append(f"Retried {cmd}: the adapter reported {e.kind}.")
                    time.sleep(self.retry_pause)

    def _by_ecu(self, mode: int, pid: int | None = None, frame: bool = False):
        return self._request(P.request(mode, pid, frame),
                             lambda r: P.response_data_by_ecu(r, mode, pid, frame))[1]

    def _pcm_data(self, mode: int, pid: int | None = None, frame: bool = False) -> bytes | None:
        res = self._by_ecu(mode, pid, frame)
        if res is None:
            return None
        data, unsupported = res
        if self.primary in unsupported:
            raise ObdNotSupported(f"The PCM does not support mode {mode:02X}.")
        return data.get(self.primary)

    def read(self) -> ObdReadout:
        self.raw = {}
        out = ObdReadout(sim=self.t.sim, read_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        self.notes, self._retries = out.notes, 0
        self.t.open()
        try:
            for cmd in INIT:
                self._ask(cmd)
            first_raw, msgs = self._request(P.request(0x01, 0x00), P.ecu_messages,
                                            self.first_timeout)     # triggers protocol search
            if msgs is None:
                raise ObdProtocolError("The vehicle did not answer. Is the key on?")
            out.protocol = self._ask("ATDPN").strip()
            if not P.is_can(out.protocol):
                raise ObdProtocolError("Only CAN vehicles are supported.")
            can = True
            self.primary = out.primary_ecu = P.primary_ecu(msgs)
            out.ecus = sorted(msgs)
            if self.primary != "7E8":
                out.notes.append(f"The engine controller answered from {self.primary}, not 7E8.")
            data, _ = P.response_data_by_ecu(first_raw, 0x01, 0x00)
            supported = P.supported_pids(0x00, data[self.primary])
            for base in (0x20, 0x40):
                if base in supported:
                    data = self._pcm_data(0x01, base)
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
        data = self._pcm_data(0x01, 0x01)
        out.mil_on = bool(data[0] & 0x80) if data else None

    def _codes(self, out: ObdReadout, can: bool) -> None:
        for status, mode in STATUS_MODES:
            res = self._by_ecu(mode)
            if res is None:
                continue
            data, unsupported = res
            for ecu in unsupported:
                out.unsupported_modes.append(f"{mode:02X}" if ecu == self.primary else f"{mode:02X} ({ecu})")
            for ecu, body in data.items():
                if ecu not in out.ecus:
                    out.ecus = sorted(out.ecus + [ecu])
                for code in P.decode_dtcs(body, can):
                    out.codes.append({"code": code, "status": status, "ecu": ecu,
                                      "description": P.describe_dtc(code) or ""})

    def _freeze(self, out: ObdReadout) -> None:
        try:
            data = self._pcm_data(0x02, 0x02, frame=True)
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
                d = self._pcm_data(0x02, pid, frame=True)
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
                    d = self._pcm_data(0x01, pid)
                except ObdNotSupported:
                    d = None
                dec = P.decode_pid(pid, d) if d else None
                value = dec[0] if dec else None
            out.live.append(LiveValue(pid, p.name, p.unit, value))
