"""11-bit CAN monitor lines as data, and best-effort decoders over their data bytes."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Frame:
    can_id: str
    dlc: int
    data: bytes


@dataclass(frozen=True)
class Decoder:
    can_id: str
    name: str
    unit: str
    version: str
    decode: Callable[[bytes], float | None]


# 3 hex ID chars, 1 DLC digit (classic CAN: 0-8), then the data; spaces optional.
_LINE = re.compile(r"([0-9A-F]{3})\s*([0-8])\s*((?:[0-9A-F]{2}\s*)*)")


def parse_line(text: str) -> Frame | None:
    m = _LINE.fullmatch(text.strip().upper())
    if m is None:
        return None
    can_id, dlc, hex_data = m.group(1), int(m.group(2)), m.group(3)
    data = bytes.fromhex(hex_data)
    if len(data) != dlc:
        return None
    return Frame(can_id, dlc, data)


def _engine_speed(b: bytes) -> float | None:
    if len(b) < 3:
        return None
    return (b[1] * 256 + b[2]) / 4


DECODERS = {d.can_id: d for d in (
    Decoder("0C9", "Engine speed", "rpm", "0C9-engine-speed-v1: bytes[1:3] BE x0.25", _engine_speed),
)}


def decode(frame: Frame) -> list[tuple[Decoder, float]]:
    dec = DECODERS.get(frame.can_id)
    if dec is None:
        return []
    value = dec.decode(frame.data)
    return [] if value is None else [(dec, value)]
