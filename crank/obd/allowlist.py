"""Read-only gate for everything sent to a real adapter. An allowlist, not a denylist:
anything not listed (ATPP, every ST* command, mode 04/09, ...) is refused."""
from __future__ import annotations

import re

from .errors import ObdForbiddenCommand

AT_ALLOWED = frozenset({"ATZ", "ATE0", "ATL0", "ATS0", "ATH0", "ATH1", "ATSP0", "ATDPN", "ATDP"})
_OBD = (re.compile(r"01[0-9A-F]{2}"),          # live PID
        re.compile(r"02[0-9A-F]{4}"),          # freeze frame: PID + frame number
        re.compile(r"(?:03|07|0A)"))           # stored / pending / permanent codes


def check(cmd: str) -> str:
    """The normalized command (stripped, uppercase, no spaces), or ObdForbiddenCommand."""
    norm = "".join(cmd.split()).upper()
    if norm in AT_ALLOWED or any(p.fullmatch(norm) for p in _OBD):
        return norm
    raise ObdForbiddenCommand(f"Refused to send {cmd.strip()!r}: only read-only OBD requests are allowed.")
