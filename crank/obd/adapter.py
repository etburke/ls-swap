"""What the adapter says it is. Pure: parsing and labelling only, no I/O."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_STN = re.compile(r"STN\d+\s+v\d+(?:\.\d+)+", re.IGNORECASE)


def clean_id(text: str, cmd: str = "") -> str:
    """The first usable line of an ID reply. Drops the prompt, blank lines, an echo of `cmd`, and the
    adapter's '?' for a command it does not know."""
    for raw in re.split(r"[\r\n]+", text.replace(">", "")):
        line = raw.strip()
        if line and line != "?" and line.upper() != cmd.strip().upper():
            return line
    return ""


def parse_sti(text: str) -> str:
    """The STN firmware string (for example 'STN2255 v4.3.1') when the reply has that shape, else ''.
    An ELM327 clone answers '?', and a bad one may answer anything, so only the shape counts."""
    m = _STN.search(text)
    return m.group(0) if m else ""


@dataclass
class AdapterInfo:
    elm_version: str = ""                  # ATI, for example "ELM327 v1.5"
    stn_version: str = ""                  # STI, for example "STN2255 v4.3.1"; empty on a plain ELM327
    device: str = ""                       # STDI, for example "OBDLink MX+"
    serial: str = ""                       # STSN
    notes: list = field(default_factory=list)

    @property
    def supports_st(self) -> bool:
        """The gate for ST features: only a reply of STN shape counts."""
        return bool(self.stn_version)

    def label(self) -> str:
        if self.supports_st:
            parts = [self.stn_version] + ([f"serial {self.serial}"] if self.serial else [])
            return f"{self.device or 'STN adapter'} ({', '.join(parts)})"
        if self.elm_version:
            return f"{self.elm_version} (not an STN adapter, no ST features)"
        return "unidentified"

    def to_dict(self) -> dict:
        return {"elm_version": self.elm_version, "stn_version": self.stn_version, "device": self.device,
                "serial": self.serial, "notes": list(self.notes)}

    @classmethod
    def from_dict(cls, d) -> "AdapterInfo":
        d = d if isinstance(d, dict) else {}

        def s(key: str) -> str:
            return str(d.get(key) or "")
        notes = d.get("notes")
        return cls(s("elm_version"), s("stn_version"), s("device"), s("serial"),
                   [str(n) for n in notes] if isinstance(notes, list) else [])
