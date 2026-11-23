"""Time series out of a saved OBD watch: which PIDs it holds, and one PID's values over time."""
from __future__ import annotations

from dataclasses import dataclass

from .pids import PIDS

RAW_SOURCE = "decoded from raw CAN frames (best-effort)"


@dataclass
class Series:
    points: list[tuple[float, float]]                  # (seconds since the watch started, value)
    name: str
    unit: str
    source: str


def _raw_rpm(meta: dict, rows: list[dict]) -> list[tuple[float, float]]:
    t0 = meta.get("started") or 0.0
    return [(r["t"] - t0, d["value"]) for r in rows for d in r.get("decoded", []) if d.get("name") == "Engine speed"]


def watch_pids(meta: dict, rows: list[dict]) -> list[int]:
    """The PIDs with at least one value: RPM (0C) first, then table order. A raw watch offers RPM only, when the
    best-effort decoder found engine speed."""
    if meta.get("mode") != "poll":
        return [0x0C] if _raw_rpm(meta, rows) else []
    have = {int(r["cmd"][2:], 16) for r in rows
            if len(r.get("cmd", "")) == 4 and r["cmd"].startswith("01") and r.get("value") is not None}
    return [p for p in PIDS if p in have and p == 0x0C] + [p for p in PIDS if p in have and p != 0x0C]


def pid_series(meta: dict, rows: list[dict], pid: int) -> Series:
    """One PID's values over time. Poll rows are official OBD answers; a raw watch's RPM is a best-effort decode."""
    p = PIDS[pid]
    if meta.get("mode") != "poll":
        return Series(_raw_rpm(meta, rows) if pid == 0x0C else [], p.name, p.unit, RAW_SOURCE)
    t0, cmd = meta.get("started") or 0.0, f"01{pid:02X}"
    pts = [(r["t"] - t0, r["value"]) for r in rows if r.get("cmd") == cmd and r.get("value") is not None]
    return Series(pts, p.name, p.unit, f"{cmd} (official)")
