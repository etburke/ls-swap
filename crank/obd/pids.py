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
)}

LIVE_PIDS = (0x0C, 0x42, 0x05, 0x0D, 0x04, 0x06, 0x07, 0x11, 0x0F, 0x0B)
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
