"""Sessions on disk: captures (.npz + sidecar JSON), manual readings, notes."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .model import CaptureData


def _safe(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip()).strip("._")
    if not s:
        raise ValueError("name must contain letters or digits")
    return s


def _write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        path.replace(path.with_suffix(path.suffix + ".corrupt"))   # keep it, start fresh
        return default


class Session:
    def __init__(self, path: Path):
        self.path = path
        self.name = path.name
        (path / "captures").mkdir(parents=True, exist_ok=True)

    # captures
    def _ids(self) -> list[str]:
        return sorted(p.stem for p in (self.path / "captures").glob("*.npz"))

    def add_capture(self, data: CaptureData, label: str = "", note: str = "") -> str:
        ids = self._ids()
        cid = f"{(int(ids[-1]) + 1) if ids else 1:03d}"
        data.save(str(self.path / "captures" / f"{cid}.npz"))
        _write_json(self.path / "captures" / f"{cid}.json", {
            "id": cid, "label": label or f"capture {cid}", "note": note,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "scenario": data.meta.get("scenario"), "sim": data.meta.get("sim", False)})
        return cid

    def list_captures(self) -> list[dict]:
        out = []
        for cid in self._ids():
            meta = _read_json(self.path / "captures" / f"{cid}.json", None)
            out.append(meta or {"id": cid, "label": f"capture {cid}", "note": "",
                                "time": "", "scenario": None, "sim": False})
        return out

    def load_capture(self, cid: str) -> CaptureData:
        return CaptureData.load(str(self.path / "captures" / f"{cid}.npz"))

    # manual readings from the guided procedure
    def readings(self) -> dict:
        return _read_json(self.path / "readings.json", {})

    def set_reading(self, step_id: str, values: dict) -> None:
        r = self.readings()
        r[step_id] = values
        _write_json(self.path / "readings.json", r)

    def notes(self) -> str:
        return _read_json(self.path / "notes.json", {}).get("text", "")

    def set_notes(self, text: str) -> None:
        _write_json(self.path / "notes.json", {"text": text})


class SessionStore:
    def __init__(self, root: str | Path = "sessions"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def list_sessions(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def open(self, name: str) -> Session:
        """Create the session if it does not exist."""
        return Session(self.root / _safe(name))
