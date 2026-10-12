"""Scope interface, the real Rigol DHO800 implementation, and the simulator."""
from __future__ import annotations

import sys
from abc import ABC, abstractmethod

import numpy as np

from .model import CaptureData, CaptureSettings


class ScopeError(Exception):
    """A scope problem the user needs to see (connection, rejected critical setting)."""


class ScopeInterface(ABC):
    def __init__(self):
        self.log: list[str] = []          # SCPI rejections and warnings, shown in the UI

    @abstractmethod
    def identify(self) -> str: ...

    @abstractmethod
    def configure(self, s: CaptureSettings) -> None: ...

    @abstractmethod
    def arm(self) -> None: ...

    @abstractmethod
    def status(self) -> str:
        """One of WAIT, RUN, AUTO, TD (triggered), STOP."""

    @abstractmethod
    def fetch(self, s: CaptureSettings) -> CaptureData: ...

    def disarm(self) -> None:
        pass

    def close(self) -> None:
        pass


# ----------------------------------------------------------------- real scope
# Commands whose rejection means the capture cannot be trusted.
CRITICAL = (":ACQuire:MDEPth", ":TIMebase", ":TRIGger", ":WAVeform:MODE", ":SINGle")


def _depth_value(text: str) -> float:
    """'10M' and '1.0E+7' both mean ten million points."""
    t = text.strip().upper()
    mult = {"K": 1e3, "M": 1e6}.get(t[-1:], 1.0)
    return float(t[:-1] if t[-1:] in ("K", "M") else t) * mult


class RigolScope(ScopeInterface):
    def __init__(self, resource: str | None = None, inst=None):
        """`inst` lets tests inject a fake VISA instrument."""
        super().__init__()
        if inst is None:
            inst = self._open(resource)
        self.i = inst
        self._requested_depth, self._depth_reported, self._srate = "10M", "?", "?"
        self.i.timeout = 30000
        self.i.chunk_size = 1 << 20

    @staticmethod
    def _open(resource):
        try:
            import pyvisa
            rm = pyvisa.ResourceManager("@py")
            if not resource:
                found = [r for r in rm.list_resources() if r.startswith(("USB", "TCPIP"))]
                rig = [r for r in found if "0X1AB1" in r.upper()] or found
                if not rig:
                    raise ScopeError("No scope found. Check the USB cable, or enter a resource "
                                     "such as TCPIP0::192.168.1.50::INSTR.")
                resource = rig[0]
            return rm.open_resource(resource)
        except ScopeError:
            raise
        except Exception as e:
            raise ScopeError(f"Could not open the scope ({e}). If another program (for example "
                             f"the Rigol tool in Claude Code) has it open, close that first.") from e

    # -- low level
    def q(self, cmd: str) -> str:
        return self.i.query(cmd).strip()

    def drain_errors(self) -> list[str]:
        errs = []
        for _ in range(8):
            e = self.q(":SYSTem:ERRor:NEXT?")
            if e.startswith("0,") or e == "0":
                break
            errs.append(e)
        return errs

    def w(self, cmd: str) -> None:
        self.i.write(cmd)
        errs = self.drain_errors()
        if not errs:
            return
        msg = f"'{cmd}' rejected: {'; '.join(errs)}"
        if cmd.startswith(CRITICAL):
            raise ScopeError(msg)
        self.log.append(msg)

    # -- interface
    def identify(self) -> str:
        return self.q("*IDN?")

    def configure(self, s: CaptureSettings) -> None:
        self.log.clear()
        self._requested_depth = s.depth
        scale = s.seconds / 10.0                       # 10 horizontal divisions
        self.w(":STOP")
        self.w(":TIMebase:MODE MAIN")
        self.w(f":TIMebase:MAIN:SCALe {scale}")
        # trigger point sits `pretrig` of the screen from the left (verify on screen)
        self.w(f":TIMebase:MAIN:OFFSet {(0.5 - s.pretrig) * 10 * scale}")
        self.w(":ACQuire:TYPE NORMal")
        self.w(f":ACQuire:MDEPth {s.depth}")
        got = self.q(":ACQuire:MDEPth?")
        self._depth_reported, self._srate = got, self.q(":ACQuire:SRATe?")
        self.log.append(f"Memory depth requested {s.depth}, scope reports {got}; "
                        f"sample rate {self._srate}")
        if _depth_value(got) != _depth_value(s.depth):
            self.log.append(f"WARNING: memory depth is {got}, not the {s.depth} requested "
                            f"(the scope may have clamped it for 4-channel mode).")
        for c in s.channels:
            self.w(f":CHANnel{c.n}:DISPlay {'ON' if c.enabled else 'OFF'}")
            if not c.enabled:
                continue
            self.w(f":CHANnel{c.n}:COUPling DC")
            self.w(f":CHANnel{c.n}:PROBe 1")
            self.w(f":CHANnel{c.n}:BWLimit OFF")
            self.w(f":CHANnel{c.n}:SCALe {c.scale}")
            self.w(f":CHANnel{c.n}:OFFSet {c.offset}")
        self.w(":TRIGger:MODE EDGE")
        self.w(":TRIGger:EDGE:SOURce CHANnel1")
        self.w(":TRIGger:EDGE:SLOPe POSitive")
        self.w(f":TRIGger:EDGE:LEVel {s.trig_level}")
        self.w(":TRIGger:SWEep SINGle")

    def self_check(self, s: CaptureSettings) -> list[tuple[bool, str]]:
        """Configure, then read every setting back and report mismatches."""
        self.configure(s)
        scale = s.seconds / 10.0
        numeric = [(":TIMebase:MAIN:SCALe?", scale, "timebase scale (s/div)"),
                   (":TIMebase:MAIN:OFFSet?", (0.5 - s.pretrig) * 10 * scale, "horizontal offset (s)"),
                   (":TRIGger:EDGE:LEVel?", s.trig_level, "trigger level (V)")]
        for c in s.channels:
            if c.enabled:
                numeric += [(f":CHANnel{c.n}:SCALe?", c.scale, f"CH{c.n} scale (V/div)"),
                            (f":CHANnel{c.n}:OFFSet?", c.offset, f"CH{c.n} offset (V)")]
        text = [(":TRIGger:EDGE:SOURce?", "CHANnel1", "trigger source"),
                (":TRIGger:EDGE:SLOPe?", "POSitive", "trigger slope"),
                (":TRIGger:SWEep?", "SINGle", "trigger sweep")]
        out = []
        for cmd, want, name in numeric:
            try:
                got = float(self.q(cmd))
                out.append((abs(got - want) <= 1e-3 * max(abs(want), 1e-6),
                            f"{name}: asked {want:g}, scope reports {got:g}"))
            except Exception as e:
                out.append((False, f"{name}: query failed ({e})"))
        try:
            got = self.q(":ACQuire:MDEPth?")
            out.append((_depth_value(got) == _depth_value(s.depth),
                        f"memory depth: asked {s.depth}, scope reports {got}"))
        except Exception as e:
            out.append((False, f"memory depth: query failed ({e})"))
        for cmd, want, name in text:
            try:
                got = self.q(cmd)
                out.append((got[:4].upper() == want[:4].upper(),
                            f"{name}: asked {want}, scope reports {got}"))
            except Exception as e:
                out.append((False, f"{name}: query failed ({e})"))
        return out

    def arm(self) -> None:
        self.w(":SINGle")

    def status(self) -> str:
        return self.q(":TRIGger:STATus?")

    def disarm(self) -> None:
        self.i.write(":STOP")

    def fetch(self, s: CaptureSettings, chunk: int = 250_000) -> CaptureData:
        channels, t = {}, None
        for c in s.channels:
            if not c.enabled:
                continue
            t, channels[c.n] = self._fetch_channel(c.n, chunk)
        if t is None:
            raise ScopeError("No channels enabled.")
        labels = {c.n: c.label for c in s.channels if c.enabled}
        points = len(t)
        want = _depth_value(self._requested_depth)
        if points < 0.9 * want:
            self.log.append(f"WARNING: capture has {points} points, fewer than the {self._requested_depth} "
                            f"requested; fine detail (runts) may be undersampled.")
        meta = {"sim": False, "idn": self.identify(), "points": points,
                "depth_requested": self._requested_depth,
                "depth_reported": self._depth_reported, "srate": self._srate}
        return CaptureData(t, channels, labels, meta)

    def _fetch_channel(self, ch: int, chunk: int):
        self.w(f":WAVeform:SOURce CHANnel{ch}")
        self.w(":WAVeform:MODE RAW")
        self.w(":WAVeform:FORMat BYTE")
        pre = [float(x) for x in self.q(":WAVeform:PREamble?").split(",")]
        n, xinc, xorg, xref, yinc, yorg, yref = (
            int(pre[2]), pre[4], pre[5], pre[6], pre[7], pre[8], pre[9])
        if n < 100_000:
            self.log.append(f"CH{ch}: preamble reports only {n} points; the scope is likely not "
                            f"returning full RAW memory (check memory depth / WAVeform:MODE RAW).")
        raw = np.empty(n, dtype=np.uint8)
        for start in range(1, n + 1, chunk):
            stop = min(start + chunk - 1, n)
            self.w(f":WAVeform:STARt {start}")
            self.w(f":WAVeform:STOP {stop}")
            d = self.i.query_binary_values(":WAVeform:DATA?", datatype="B", container=np.array)
            raw[start - 1:stop] = d[: stop - start + 1]
        v = (raw.astype(np.float32) - yorg - yref) * yinc
        t = (np.arange(n) - xref) * xinc + xorg
        return t, v

    def close(self) -> None:
        try:
            self.i.close()
        except Exception:
            pass


# ----------------------------------------------------------------- simulator
class SimScope(ScopeInterface):
    """Plays back a generated scenario. `fault` injects capture-flow failures:
    None, "never_arms" (status stays STOP), "stuck_waiting" (never triggers),
    or "fetch_error" (download raises)."""

    def __init__(self, scenario: str = "healthy", arm_polls: int = 3,
                 fault: str | None = None, seed: int = 1):
        super().__init__()
        self.scenario, self.arm_polls, self.fault, self.seed = scenario, arm_polls, fault, seed
        self._polls = 0
        self._armed = False

    def identify(self) -> str:
        return f"SIMULATOR,{self.scenario}"

    def configure(self, s: CaptureSettings) -> None:
        self.log.append(f"Simulator configured for scenario '{self.scenario}'.")

    def arm(self) -> None:
        self._polls, self._armed = 0, self.fault != "never_arms"

    def status(self) -> str:
        if not self._armed:
            return "STOP"
        if self.fault == "stuck_waiting":
            return "WAIT"
        self._polls += 1
        if self._polls <= self.arm_polls:
            return "WAIT"
        if self._polls == self.arm_polls + 1:
            return "TD"
        return "STOP"

    def disarm(self) -> None:
        self._armed = False

    def fetch(self, s: CaptureSettings) -> CaptureData:
        from .sim import generate
        if self.fault == "fetch_error":
            raise ScopeError("Simulated download failure.")
        d = generate(self.scenario, seconds=s.seconds, pretrig=s.pretrig, seed=self.seed)
        keep = {c.n for c in s.channels if c.enabled}
        d.channels = {n: v for n, v in d.channels.items() if n in keep}
        d.labels = {c.n: c.label for c in s.channels if c.enabled}
        return d
