"""The adapter traffic log: every command, reply, error and connection event, with timestamps.
Diagnostic only; it never goes into a case."""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from .errors import ObdError
from .transport import DelegatingTransport

_HB = re.compile(r"^\S+ \[[^\]]*\] hb ")


def _stamp(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + f".{int(t * 1000) % 1000:03d}"


class _File:
    """An append-only file that rotates: `keep` files in all (name, name.1, name.2 for keep=3)."""

    def __init__(self, path: Path, max_bytes: int, keep: int):
        self.path, self.max_bytes, self.keep = Path(path), max_bytes, keep

    def write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
            self._rotate()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(text)

    def _rotate(self) -> None:
        for i in range(self.keep - 1, 0, -1):
            src = self.path if i == 1 else self.path.with_name(f"{self.path.name}.{i - 1}")
            if src.exists():
                src.replace(self.path.with_name(f"{self.path.name}.{i}"))


class TrafficLog:
    def __init__(self, logs_dir, session=None, max_bytes: int = 5_000_000, keep: int = 3, now=time.time):
        self.path = Path(logs_dir) / "obd.log"
        self._app = _File(self.path, max_bytes, keep)
        self._session, self._max, self._keep, self._now = session, max_bytes, keep, now
        self._lock = threading.Lock()
        self.errors, self.last_error = 0, ""

    def _fail(self, e: Exception) -> None:
        self.errors += 1
        self.last_error = f"{type(e).__name__}: {e}"

    def _write(self, kind: str, text: str, tag: str) -> None:
        name, sdir = (self._session() if self._session else None) or ("", None)
        line = f"{_stamp(self._now())} [{name or '-'}] {tag + ' ' if tag else ''}{kind} {text}\n"
        with self._lock:
            try:
                self._app.write(line)
            except OSError as e:                      # a log must never break a read
                self._fail(e)
            if sdir is not None and tag != "hb":
                try:
                    _File(Path(sdir) / "obd.log", self._max, self._keep).write(line)
                except OSError as e:
                    self._fail(e)

    def event(self, text: str, tag: str = "") -> None:
        self._write("**", text, tag)

    def command(self, cmd: str, tag: str = "") -> None:
        self._write(">>", cmd, tag)

    def reply(self, text: str, ms: float, tag: str = "") -> None:
        body = " | ".join(p for p in re.split(r"[\r\n]+", text) if p.strip()) or "(empty)"
        self._write("<<", f"{body} ({ms:.0f} ms)", tag)

    def error(self, exc: Exception, tag: str = "") -> None:
        self._write("!!", f"{type(exc).__name__}: {exc}", tag)


class LoggingTransport(DelegatingTransport):
    """Records every command, reply and error of the transport it wraps."""

    def __init__(self, inner, log: TrafficLog, tag: str = ""):
        super().__init__(inner)
        self.log, self.tag = log, tag

    def _tag(self) -> str:
        return self.tag or ("sim" if self.inner.sim else "")

    def open(self) -> None:
        self.log.event("port open", self._tag())
        self.inner.open()

    def send(self, cmd: str, timeout: float = 5.0) -> str:
        self.log.command(cmd, self._tag())
        start = time.monotonic()
        try:
            reply = self.inner.send(cmd, timeout)
        except ObdError as e:
            self.log.error(e, self._tag())
            raise
        self.log.reply(reply, (time.monotonic() - start) * 1000, self._tag())
        return reply

    def close(self) -> None:
        self.log.event("port close", self._tag())
        self.inner.close()


def tail_lines(path, n: int = 200, hide_heartbeats: bool = False) -> list[str]:
    """The last `n` lines of the file (after dropping heartbeat lines when asked), read from the end."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return []
    block = 64 * 1024
    while True:
        take = min(size, block)
        with open(path, "rb") as f:
            f.seek(size - take)
            data = f.read(take).decode("utf-8", errors="replace")
        got = data.splitlines()
        if take < size:
            got = got[1:]                              # the first line may be cut off
        if hide_heartbeats:
            got = [l for l in got if not _HB.match(l)]
        if len(got) >= n or take >= size:
            return got[-n:] if n else []
        block *= 4
