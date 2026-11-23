"""Mode 01 / 02 PIDs as data: id, name, unit, decoder over the data bytes after the PID."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Pid:
    id: int
    name: str
    unit: str
    nbytes: int
    decode: Callable[[bytes], float]


def _u16(b: bytes) -> int:
    return b[0] * 256 + b[1]


PIDS = {p.id: p for p in (
    Pid(0x04, "Engine load", "%", 1, lambda b: b[0] * 100 / 255),
    Pid(0x05, "Coolant temperature", "°C", 1, lambda b: b[0] - 40),
    Pid(0x06, "Short term fuel trim bank 1", "%", 1, lambda b: b[0] * 100 / 128 - 100),
    Pid(0x07, "Long term fuel trim bank 1", "%", 1, lambda b: b[0] * 100 / 128 - 100),
    Pid(0x0B, "Intake manifold pressure", "kPa", 1, lambda b: b[0]),
    Pid(0x0C, "Engine RPM", "rpm", 2, lambda b: _u16(b) / 4),
    Pid(0x0D, "Vehicle speed", "km/h", 1, lambda b: b[0]),
    Pid(0x0F, "Intake air temperature", "°C", 1, lambda b: b[0] - 40),
    Pid(0x11, "Throttle position", "%", 1, lambda b: b[0] * 100 / 255),
    Pid(0x42, "Control module voltage", "V", 2, lambda b: _u16(b) / 1000),
    Pid(0x08, "Short term fuel trim bank 2", "%", 1, lambda b: b[0] * 100 / 128 - 100),
    Pid(0x09, "Long term fuel trim bank 2", "%", 1, lambda b: b[0] * 100 / 128 - 100),
    Pid(0x0E, "Timing advance", "° BTDC", 1, lambda b: b[0] / 2 - 64),
    Pid(0x10, "Mass air flow", "g/s", 2, lambda b: _u16(b) / 100),
    Pid(0x1F, "Run time since start", "s", 2, lambda b: _u16(b)),
    Pid(0x33, "Barometric pressure", "kPa", 1, lambda b: b[0]),
    Pid(0x43, "Absolute load", "%", 2, lambda b: _u16(b) * 100 / 255),
    Pid(0x44, "Commanded equivalence ratio", "λ", 2, lambda b: _u16(b) * 2 / 65536),
    Pid(0x45, "Relative throttle position", "%", 1, lambda b: b[0] * 100 / 255),
    Pid(0x46, "Ambient air temperature", "°C", 1, lambda b: b[0] - 40),
    Pid(0x47, "Throttle position B", "%", 1, lambda b: b[0] * 100 / 255),
    Pid(0x49, "Accelerator pedal position D", "%", 1, lambda b: b[0] * 100 / 255),
    Pid(0x4A, "Accelerator pedal position E", "%", 1, lambda b: b[0] * 100 / 255),
    Pid(0x4C, "Commanded throttle actuator", "%", 1, lambda b: b[0] * 100 / 255),
)}

LIVE_PIDS = (0x0C, 0x42, 0x05, 0x0D, 0x04, 0x06, 0x07, 0x11, 0x0F, 0x0B)
POLL_PIDS = tuple(p for p in PIDS if p != 0x0C)     # everything a watch polls beside RPM, in table order


def read_order(supported) -> list[int]:
    """What a read asks for: the classic LIVE_PIDS (always listed, supported or not), then every other PID in the
    table that `supported` contains, in table order."""
    return list(LIVE_PIDS) + [p for p in PIDS if p not in LIVE_PIDS and p in supported]


def poll_groups(supported) -> list[tuple[int, ...]]:
    """Requests a poll watch cycles through: the supported POLL_PIDS in fives, each led by RPM (0C), so every
    request carries RPM and none carries more than the six PIDs a CAN request allows."""
    others = [p for p in POLL_PIDS if p in supported]
    if not others:
        return [(0x0C,)]
    return [(0x0C, *others[i:i + 5]) for i in range(0, len(others), 5)]


FREEZE_PIDS = (0x0C, 0x05, 0x04, 0x06, 0x07, 0x0B, 0x0D, 0x0F, 0x11)

DTC_DESCRIPTIONS = {
    "P0016": "Crankshaft/camshaft position correlation, bank 1 sensor A",
    "P0335": "Crankshaft position sensor A circuit",
    "P0336": "Crankshaft position sensor A range/performance",
    "P0340": "Camshaft position sensor A circuit, bank 1",
    "P0341": "Camshaft position sensor A range/performance, bank 1",
    "P2119": "Throttle actuator control throttle body range/performance",
    "P0300": "Random/multiple cylinder misfire detected",
    "P0171": "System too lean, bank 1",
    "P0174": "System too lean, bank 2",
    "U0100": "Lost communication with ECM/PCM",
}
