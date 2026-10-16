"""Transport interface for an ELM327-style adapter, and a simulated one for tests and demos."""
from __future__ import annotations

import copy
from itertools import zip_longest
from dataclasses import dataclass, field

from .errors import ObdNoAdapter, ObdTimeout
from .protocol import request


class Transport:
    """One command in, the adapter's reply text out (everything up to the '>' prompt)."""

    sim = False

    def open(self) -> None:
        raise NotImplementedError

    def send(self, cmd: str, timeout: float = 5.0) -> str:
        raise NotImplementedError

    def close(self) -> None:
        pass


def _enc_pid_bitmap(pids: set[int], base: int) -> bytes:
    out = bytearray(4)
    for p in pids:
        n = p - base - 1
        if 0 <= n < 32:
            out[n // 8] |= 0x80 >> (n % 8)
    return bytes(out)


def _dtc_bytes(code: str) -> bytes:
    b0 = ("PCBU".index(code[0]) << 6) | (int(code[1]) << 4) | int(code[2], 16)
    return bytes([b0, int(code[3:5], 16)])


def _frames(msg: bytes) -> str:
    """CAN framing as an ELM327 prints it with headers off: single line, or length + indexed lines."""
    if len(msg) <= 7:
        return msg.hex().upper()
    chunks = [msg[:6]] + [msg[i:i + 7] for i in range(6, len(msg), 7)]
    lines = [f"{len(msg):03X}"] + [f"{i}: {' '.join(f'{b:02X}' for b in c)}" for i, c in enumerate(chunks)]
    return "\r".join(lines)


def _isotp(msg: bytes) -> list[bytes]:
    """Raw 8-byte CAN data fields for one message: a single frame, or first + consecutive frames."""
    if len(msg) <= 7:
        return [(bytes([len(msg)]) + msg).ljust(8, b"\x00")]
    out = [bytes([0x10 | (len(msg) >> 8), len(msg) & 0xFF]) + msg[:6]]
    for i, off in enumerate(range(6, len(msg), 7), start=1):
        out.append((bytes([0x20 | (i & 0xF)]) + msg[off:off + 7]).ljust(8, b"\x00"))
    return out


@dataclass
class Scenario:
    stored: list = field(default_factory=list)
    pending: list = field(default_factory=list)
    permanent: list = field(default_factory=list)
    mil: bool = False
    freeze_dtc: str | None = None
    values: dict = field(default_factory=dict)         # pid -> data bytes (the supported PIDs)
    freeze_values: dict = field(default_factory=dict)
    protocol: str = "A6"
    unsupported_modes: tuple = ()                      # modes the PCM answers with NO DATA / '?'
    pcm_header: str = "7E8"
    other_ecus: dict = field(default_factory=dict)     # header -> {"stored","pending","permanent","unsupported_modes"}
    interleave: bool = False                           # frames from several ECUs alternate on the wire
    search_delay: float = 0.0                          # seconds the first OBD request takes (protocol search)
    error_on: str = ""                                 # an OBD request that draws an adapter error
    error_text: str = ""                               # e.g. "CAN ERROR"
    error_times: int = 1                               # how many times; 99 = persistent
    fault: str | None = None                           # silent | no_adapter | no_vehicle | garbage | drop_after:N


LIVE_RUNNING = {0x0C: bytes([0x0B, 0xB8]), 0x42: bytes([0x36, 0xB0]), 0x05: bytes([0x82]), 0x0D: bytes([0]),
                0x04: bytes([0x40]), 0x06: bytes([0x80]), 0x07: bytes([0x84]), 0x11: bytes([0x30]),
                0x0F: bytes([0x4B]), 0x0B: bytes([0x23])}
LIVE_CRANKING = {**LIVE_RUNNING, 0x0C: bytes([0, 0]), 0x42: bytes([0x2E, 0xE0])}

SCENARIOS = {
    "healthy": Scenario(values=LIVE_RUNNING),
    "p2119_zero_rpm": Scenario(stored=["P2119"], mil=True, freeze_dtc="P2119", values=LIVE_CRANKING,
                               freeze_values={0x0C: bytes([0, 0]), 0x05: bytes([0x82])}),
    "many_codes": Scenario(stored=["P0335", "P0336", "P0016"], pending=["P0340"], permanent=["P0335"],
                           mil=True, freeze_dtc="P0335", values=LIVE_RUNNING,
                           freeze_values={0x0C: bytes([0x0B, 0xB8])}),
    "limited_pcm": Scenario(stored=["P2119"], mil=True, values={0x0C: bytes([0, 0]), 0x05: bytes([0x82])},
                            unsupported_modes=(0x0A, 0x02)),
    "pcm_and_trans": Scenario(stored=["P2119"], mil=True, freeze_dtc="P2119", values=LIVE_CRANKING,
                              freeze_values={0x0C: bytes([0, 0]), 0x05: bytes([0x82])},
                              other_ecus={"7E9": {"stored": ["P0700"]}}),
    "interleaved_multiframe": Scenario(stored=["P0335", "P0336", "P0016"], values=LIVE_RUNNING, interleave=True,
                                       other_ecus={"7E9": {"stored": ["P0700", "P0701", "P0702"]}}),
    "same_code_two_ecus": Scenario(stored=["P0335"], values=LIVE_RUNNING,
                                   other_ecus={"7E9": {"stored": ["P0335"]}}),
    "pcm_not_7e8": Scenario(stored=["P2119"], values=LIVE_RUNNING, pcm_header="7E9"),
    "trans_rejects_mode": Scenario(stored=["P2119"], values=LIVE_RUNNING,
                                   other_ecus={"7E9": {"stored": ["P0700"], "unsupported_modes": (0x0A,)}}),
    "slow_search": Scenario(values=LIVE_RUNNING, search_delay=8.0),
    "transient_can_error": Scenario(values=LIVE_RUNNING, error_on="010C", error_text="CAN ERROR"),
    "persistent_can_error": Scenario(values=LIVE_RUNNING, error_on="010C", error_text="CAN ERROR",
                                     error_times=99),
    "lv_reset_mid_read": Scenario(values=LIVE_RUNNING, error_on="010C", error_text="LV RESET"),
    "buffer_full": Scenario(values=LIVE_RUNNING, error_on="010C", error_text="BUFFER FULL"),
    "silent_adapter": Scenario(fault="silent"),
    "no_vehicle": Scenario(fault="no_vehicle"),
    "garbage": Scenario(fault="garbage"),
    "drops_mid_read": Scenario(values=LIVE_RUNNING, fault="drop_after:8"),
}


class SimTransport(Transport):
    sim = True

    def __init__(self, scenario: str = "healthy"):
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown OBD scenario {scenario!r}")
        self.name, self.sc, self.n, self.opened = scenario, copy.deepcopy(SCENARIOS[scenario]), 0, False
        self.headers = False
        self.searched, self.errors_left = False, self.sc.error_times

    def open(self) -> None:
        if self.sc.fault == "no_adapter":
            raise ObdNoAdapter("No OBD adapter found.")
        self.opened = True

    def send(self, cmd: str, timeout: float = 5.0) -> str:
        if not self.opened:
            raise ObdNoAdapter("The adapter is not open.")
        self.n += 1
        f = self.sc.fault
        if f == "silent":
            raise ObdTimeout("The adapter did not answer. Check it is plugged in and powered.")
        if f and f.startswith("drop_after:") and self.n > int(f.split(":")[1]):
            raise ObdTimeout("The adapter stopped answering partway through the read.")
        cmd = cmd.strip().upper().replace(" ", "")
        if cmd.startswith("AT"):
            return self._at(cmd)
        if f == "garbage":
            return "@#$%^ \r\x00\x01"
        if f == "no_vehicle":
            return "UNABLE TO CONNECT"
        if not self.searched:
            if self.sc.search_delay > timeout:
                raise ObdTimeout("The adapter did not answer. Check it is plugged in and powered.")
            self.searched = True
        if cmd == self.sc.error_on and self.errors_left > 0:
            self.errors_left -= 1
            if self.sc.error_text == "LV RESET":               # the adapter forgot its settings
                self.headers, self.searched = False, False
            return self.sc.error_text
        return self._obd(cmd)

    def _at(self, cmd: str) -> str:
        if cmd == "ATZ":
            self.headers, self.searched = False, False
            return "ELM327 v1.5"
        if cmd in ("ATH0", "ATH1"):
            self.headers = cmd == "ATH1"
        if cmd == "ATSP0":
            self.searched = False
        if cmd == "ATDPN":
            return self.sc.protocol
        return "OK"

    def _obd(self, cmd: str) -> str:
        sc = self.sc
        mode, rest = int(cmd[:2], 16), cmd[2:]
        pid = int(rest[:2], 16) if rest else None
        body = self._pcm_body(mode, pid)
        if body == "?":
            return "?"
        items = [(sc.pcm_header, body)] if body is not None else []
        if mode in (0x03, 0x07, 0x0A):
            for ecu, o in sc.other_ecus.items():
                b = self._other_body(o, mode)
                if b is not None:
                    items.append((ecu, b))
        if not items:
            return "NO DATA"
        if not self.headers:
            return _frames(items[0][1])
        per = [[ecu + fr.hex().upper() for fr in _isotp(b)] for ecu, b in items]
        order = ([x for group in zip_longest(*per) for x in group if x] if sc.interleave
                 else [x for group in per for x in group])
        return "\r".join(order)

    @staticmethod
    def _other_body(o: dict, mode: int) -> bytes | None:
        if mode in tuple(o.get("unsupported_modes", ())):
            return bytes([0x7F, mode, 0x11])
        codes = o.get({0x03: "stored", 0x07: "pending", 0x0A: "permanent"}[mode], [])
        if not codes:
            return None
        return bytes([mode + 0x40, len(codes)]) + b"".join(_dtc_bytes(c) for c in codes)

    def _pcm_body(self, mode: int, pid: int | None):
        """The PCM's message for this request: bytes, None for NO DATA, or '?' for an unknown mode."""
        sc = self.sc
        if mode in sc.unsupported_modes:
            return bytes([0x7F, mode, 0x11])
        if mode in (0x03, 0x07, 0x0A):
            codes = {0x03: sc.stored, 0x07: sc.pending, 0x0A: sc.permanent}[mode]
            if not codes:
                return None
            return bytes([mode + 0x40, len(codes)]) + b"".join(_dtc_bytes(c) for c in codes)
        if mode == 0x01:
            if pid in (0x00, 0x20, 0x40):
                sup = set(sc.values) | {0x01}
                if pid == 0x00 and any(p > 0x20 for p in sup):
                    sup.add(0x20)
                if pid == 0x20 and any(p > 0x40 for p in sup):
                    sup.add(0x40)
                return bytes([0x41, pid]) + _enc_pid_bitmap(sup, pid)
            if pid == 0x01:
                return bytes([0x41, 0x01, (0x80 if sc.mil else 0) | len(sc.stored), 0, 0, 0])
            if pid in sc.values:
                return bytes([0x41, pid]) + sc.values[pid]
            return None
        if mode == 0x02:
            if pid == 0x02:
                return bytes([0x42, 0x02, 0]) + _dtc_bytes(sc.freeze_dtc) if sc.freeze_dtc else None
            if pid in sc.freeze_values:
                return bytes([0x42, pid, 0]) + sc.freeze_values[pid]
            return None
        return "?"


__all__ = ["Transport", "SimTransport", "SCENARIOS", "request"]
