"""Read-only gate for everything sent to a real adapter. An allowlist, not a denylist:
mode 01 takes one to six PIDs per request; anything not listed (ATPP, every ST* command except the three identification ones, mode 04/09, ...)
is refused.

Monitor mode (`ATMA`) and its display settings are listen-only; `ATCSM1` keeps the adapter silent on the bus (no ACKs).
Added 2026-11-20 for the OBD watch."""
from __future__ import annotations

import re

from .errors import ObdForbiddenCommand

AT_ALLOWED = frozenset({"ATZ", "ATE0", "ATL0", "ATS0", "ATH0", "ATH1", "ATSP0", "ATDPN", "ATDP", "ATI", "ATRV",
                        "ATMA", "ATCSM1", "ATCAF0", "ATCAF1", "ATD0", "ATD1"})
ST_ALLOWED = frozenset({"STI", "STDI", "STSN"})        # identify the adapter; nothing else under ST*
_OBD = (re.compile(r"01(?:[0-9A-F]{2}){1,6}"),  # live PIDs: one to six per request (CAN allows six)
        re.compile(r"02[0-9A-F]{4}"),          # freeze frame: PID + frame number
        re.compile(r"(?:03|07|0A)"))           # stored / pending / permanent codes


def check(cmd: str) -> str:
    """The normalized command (stripped, uppercase, no spaces), or ObdForbiddenCommand."""
    norm = "".join(cmd.split()).upper()
    if norm in AT_ALLOWED or norm in ST_ALLOWED or any(p.fullmatch(norm) for p in _OBD):
        return norm
    raise ObdForbiddenCommand(f"Refused to send {cmd.strip()!r}: only read-only OBD requests are allowed.")
