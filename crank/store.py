"""Sessions on disk: captures (.npz + sidecar JSON), manual readings, notes."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

from .model import CaptureData
from .roles import check_role, legacy_meta


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


_ID = re.compile(r"\d{3,}")


class WatchWriter:
    """One watch being recorded: rows go to NNN.jsonl as they arrive (flushed at least every 0.5 s, so a
    crash keeps them); finish() writes the NNN.json meta, discard() removes both."""

    FLUSH_S = 0.5

    def __init__(self, folder: Path, wid: str):
        self.id = wid
        self._rows, self._meta = folder / f"{wid}.jsonl", folder / f"{wid}.json"
        self._f = open(self._rows, "x", encoding="utf-8")
        self._flushed = 0.0                          # the first row goes to disk at once

    def record(self, row: dict) -> None:
        self._f.write(json.dumps(row) + "\n")
        now = time.monotonic()
        if now - self._flushed >= self.FLUSH_S:
            self._f.flush()
            self._flushed = now

    def finish(self, meta: dict) -> None:
        self._f.close()
        _write_json(self._meta, meta)

    def discard(self) -> None:
        self._f.close()
        self._rows.unlink(missing_ok=True)
        self._meta.unlink(missing_ok=True)


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
            row = meta or {"id": cid, "label": f"capture {cid}", "note": "",
                           "time": "", "scenario": None, "sim": False}
            row = {k: v for k, v in row.items() if k != "roles"}
            row["roles_corrected"] = bool(meta and meta.get("roles"))
            out.append(row)
        return out

    def load_capture(self, cid: str) -> CaptureData:
        """The capture, with meta["roles"] the effective channel roles and meta["roles_source"] saying where they
        came from: "corrected" (the capture's .json), "saved" (recorded with the scope data) or "legacy" (the
        layout every capture had before roles)."""
        data = CaptureData.load(str(self.path / "captures" / f"{cid}.npz"))
        corrected = (_read_json(self.path / "captures" / f"{cid}.json", None) or {}).get("roles")
        if corrected:
            data.meta["roles"], data.meta["roles_source"] = corrected, "corrected"
        elif data.meta.get("roles"):
            data.meta["roles_source"] = "saved"
        else:
            data.meta["roles"], data.meta["roles_source"] = legacy_meta(data.channels), "legacy"
        return data

    def set_capture_roles(self, cid: str, roles: dict) -> None:
        """Correct a saved capture's channel roles ({channel: (role, num)}). Stored in the capture's .json; the
        scope data is never rewritten. Channels not named keep their current role."""
        data = self.load_capture(cid)
        merged = {k: dict(v) for k, v in data.meta["roles"].items()}
        for n, (key, num) in roles.items():
            if int(n) not in data.channels:
                raise ValueError(f"Capture {cid} has no CH{int(n)}.")
            key, num = check_role(key, num)
            merged[str(int(n))] = {"role": key, "num": num}
        path = self.path / "captures" / f"{cid}.json"
        meta = _read_json(path, None) or {"id": cid, "label": f"capture {cid}", "note": "", "time": "",
                                         "scenario": data.meta.get("scenario"), "sim": data.meta.get("sim", False)}
        meta["roles"] = merged
        _write_json(path, meta)

    # OBD watches: watches/NNN.jsonl (rows) + watches/NNN.json (meta, written when the watch is kept)
    def _watch_dir(self) -> Path:
        return self.path / "watches"

    def new_watch(self) -> WatchWriter:
        folder = self._watch_dir()
        folder.mkdir(exist_ok=True)
        nums = [int(p.name.split(".")[0]) for p in folder.glob("*.json*") if _ID.fullmatch(p.name.split(".")[0])]
        return WatchWriter(folder, f"{max(nums, default=0) + 1:03d}")

    def list_watches(self) -> list[dict]:
        folder = self._watch_dir()
        if not folder.is_dir():
            return []
        metas = (_read_json(p, None) for p in sorted(folder.glob("*.json")) if _ID.fullmatch(p.stem))
        return [m for m in metas if m]

    def _watch_meta(self, wid: str) -> dict:
        meta = _read_json(self._watch_dir() / f"{wid}.json", None) if _ID.fullmatch(wid) else None
        if not meta:
            raise KeyError(wid)
        return meta

    def load_watch(self, wid: str) -> tuple[dict, list[dict]]:
        meta, rows = self._watch_meta(wid), []
        try:
            text = (self._watch_dir() / f"{wid}.jsonl").read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        for line in text.splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:                 # a line cut short by a crash
                pass
        return meta, rows

    def link_watch(self, wid: str, capture_id: str) -> None:
        meta = self._watch_meta(wid)
        cap_path = self.path / "captures" / f"{capture_id}.json"
        cap = _read_json(cap_path, None) if _ID.fullmatch(capture_id) else None
        if not cap:
            raise KeyError(capture_id)
        meta["capture_id"], cap["watch_id"] = capture_id, wid
        _write_json(self._watch_dir() / f"{wid}.json", meta)
        _write_json(cap_path, cap)

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

    def delete_session(self, name: str) -> None:
        """Remove a session folder and everything in it. KeyError if there is no such session."""
        path = self.root / _safe(name)
        if not path.is_dir():
            raise KeyError(name)
        shutil.rmtree(path)
