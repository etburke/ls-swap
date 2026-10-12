"""Pure ELM327 / OBD-II parsing and request building. No I/O."""
from __future__ import annotations

import re

from .errors import ObdNotSupported, ObdProtocolError
from .pids import DTC_DESCRIPTIONS, PIDS

ERRORS = ("UNABLE TO CONNECT", "BUS INIT", "CAN ERROR", "BUS BUSY", "BUFFER FULL", "STOPPED",
          "FB ERROR", "DATA ERROR", "ERR", "ACT ALERT", "LV RESET")
NOISE = ("SEARCHING", "ELM327", "OK")
_HEX = re.compile(r"^[0-9A-F]+$")


def _lines(resp: str) -> list[str]:
    out = []
    for raw in re.split(r"[\r\n]+", resp.replace(">", "")):
        line = raw.strip().upper()
        if line and not any(line.startswith(n) for n in NOISE):
            out.append(line)
    return out


def messages(resp: str) -> list[bytes] | None:
    """Reassemble the ECU messages in a response. None means 'NO DATA'.

    Raises ObdProtocolError for adapter errors and for text that is not hex.
    """
    lines = _lines(resp)
    if not lines or lines == ["NO DATA"]:
        return None
    for line in lines:
        if line == "?":
            raise ObdProtocolError("The adapter did not understand the command.")
        if any(line.startswith(e) for e in ERRORS):
            raise ObdProtocolError(f"The adapter reported: {line.title()}.")
    msgs, i = [], 0
    while i < len(lines):
        line = lines[i]
        if re.fullmatch(r"[0-9A-F]{3}", line) and i + 1 < len(lines) and lines[i + 1].startswith("0:"):
            want, body, i = int(line, 16), [], i + 1
            while i < len(lines) and re.match(r"^\d+:", lines[i]):
                body.append(lines[i].split(":", 1)[1].replace(" ", ""))
                i += 1
            msgs.append(_hex("".join(body))[:want])
            continue
        msgs.append(_hex(line.replace(" ", "")))
        i += 1
    return msgs


def _hex(s: str) -> bytes:
    if not _HEX.match(s) or len(s) % 2:
        raise ObdProtocolError(f"Unreadable response from the adapter: {s[:24]!r}.")
    return bytes.fromhex(s)


_NOT_SUPPORTED_NRC = (0x11, 0x12, 0x31)    # service / sub-function not supported, request out of range
_NRC_TEXT = {0x21: "busy / conditions not correct", 0x22: "busy / conditions not correct",
             0x78: "response pending"}


def request(mode: int, pid: int | None = None, frame: bool = False) -> str:
    """e.g. request(1, 0x0C) -> '010C'; request(2, 0x0C, frame=True) -> '020C00'."""
    s = f"{mode:02X}" + (f"{pid:02X}" if pid is not None else "")
    return s + ("00" if frame else "")


def response_data(resp: str, mode: int, pid: int | None = None, frame: bool = False) -> bytes | None:
    """The data bytes of the message answering this mode/PID (a positive one wins); None for NO DATA."""
    msgs = messages(resp)
    if msgs is None:
        return None
    head = bytes([mode + 0x40] + ([pid] if pid is not None else []) + ([0] if frame else []))
    for m in msgs:
        if m.startswith(head):
            return m[len(head):]
    negatives = [m for m in msgs if len(m) >= 3 and m[0] == 0x7F and m[1] == mode]
    for m in negatives:
        if m[2] in _NOT_SUPPORTED_NRC:
            raise ObdNotSupported(f"The PCM does not support mode {mode:02X} (negative response {m[2]:02X}).")
    if negatives:
        nrc = negatives[0][2]
        what = _NRC_TEXT.get(nrc, "an error")
        raise ObdProtocolError(f"The PCM answered '{what}' (negative response {nrc:02X}) to mode {mode:02X}; "
                               "try again.")
    raise ObdProtocolError(f"Unexpected reply to mode {mode:02X}: {msgs[0].hex().upper()}.")


def decode_dtc(b0: int, b1: int) -> str:
    letter = "PCBU"[b0 >> 6]
    return f"{letter}{(b0 >> 4) & 3}{b0 & 0xF:X}{b1 >> 4:X}{b1 & 0xF:X}"


def decode_dtcs(data: bytes, can: bool) -> list[str]:
    """DTC list from a mode 03/07/0A reply body. CAN replies start with a count byte."""
    if can and data:
        data = data[1:]
    codes = []
    for i in range(0, len(data) - 1, 2):
        if data[i] == 0 and data[i + 1] == 0:
            continue
        codes.append(decode_dtc(data[i], data[i + 1]))
    return codes


def describe_dtc(code: str) -> str | None:
    return DTC_DESCRIPTIONS.get(code)


def supported_pids(base: int, data: bytes) -> set[int]:
    """PIDs base+1..base+32 flagged in a 4-byte support bitmap."""
    out = set()
    for n in range(32):
        if n // 8 < len(data) and data[n // 8] & (0x80 >> (n % 8)):
            out.add(base + n + 1)
    return out


def decode_pid(pid: int, data: bytes):
    """(value, unit) for a known PID, or None if the data is too short."""
    p = PIDS[pid]
    if len(data) < p.nbytes:
        return None
    return p.decode(data[:p.nbytes]), p.unit


def is_can(protocol_text: str) -> bool:
    """ATDPN reply such as 'A6' or '6': protocols 6-9 are CAN."""
    t = protocol_text.strip().upper().lstrip("A")
    return t[:1] in ("6", "7", "8", "9")
