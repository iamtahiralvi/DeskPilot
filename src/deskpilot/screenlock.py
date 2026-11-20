"""One AI at a time: a PC-wide lock so two agent sessions never drive the mouse and keyboard at once.

The mouse, keyboard and focused window are shared by every screen, so there is one lock for the whole computer.
An agent claims it on its first action of a task and keeps it until done() (or until the overlay hides when the
agent goes quiet). Reading the screen never needs it. A lock whose owner process is gone, or that has not been
refreshed for STALE_AFTER seconds, is taken over.
"""
from __future__ import annotations

import json
import os
import time

from .config import HOME

LOCK = HOME / "screen.lock"
STALE_AFTER = 300          # seconds without a heartbeat before a lock counts as abandoned
_HEARTBEAT_EVERY = 5


class Busy(Exception):
    pass


def _read() -> dict | None:
    try:
        return json.loads(LOCK.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _alive(pid) -> bool:
    try:
        import psutil

        return psutil.pid_exists(int(pid)) and psutil.Process(int(pid)).status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def stale(cur: dict, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return not _alive(cur.get("pid")) or now - float(cur.get("beat", 0)) > STALE_AFTER


def session_label() -> str:
    """'claude in myproject': the AI client that started this server and the folder it works in."""
    try:
        import psutil

        parents = psutil.Process(os.getpid()).parents()
    except Exception:
        parents = []
    for p in parents:
        try:  # one unreadable ancestor must not hide the rest
            name = p.name().lower()
            if any(s in name for s in ("python", "deskpilot", "uv", "cmd.exe", "conhost", "timeout", "bash",
                                       "sh.exe", "powershell", "pwsh")):
                continue
            try:
                folder = os.path.basename(p.cwd().rstrip("\\/")) or p.cwd()
            except Exception:
                folder = ""
            return f"{name.removesuffix('.exe')}" + (f" in {folder}" if folder else "")
        except Exception:
            continue
    return f"session {os.getpid()}"


class ScreenLock:
    def __init__(self, label: str | None = None):
        self.pid = os.getpid()
        self.label = label or session_label()
        self.held = False
        self._beat = 0.0

    def _write(self, since: float) -> None:
        HOME.mkdir(parents=True, exist_ok=True)
        tmp = LOCK.with_name(f"screen.lock.{self.pid}")
        tmp.write_text(json.dumps({"pid": self.pid, "label": self.label, "since": since, "beat": time.time()}),
                       encoding="utf-8")
        os.replace(tmp, LOCK)
        self._beat = time.time()

    def claim(self, wait: float = 5.0) -> None:
        """Take (or refresh) the lock. Raises Busy if another live session holds it for longer than `wait`."""
        deadline = time.time() + wait
        while True:
            cur = _read()
            if cur and cur.get("pid") == self.pid:
                if time.time() - self._beat > _HEARTBEAT_EVERY:
                    self._write(float(cur.get("since", time.time())))
                self.held = True
                return
            if cur is None or stale(cur):
                self._write(time.time())
                time.sleep(0.05)               # two sessions racing: the last writer wins, the other re-checks
                again = _read()
                if again and again.get("pid") == self.pid:
                    self.held = True
                    return
                continue
            if time.time() >= deadline:
                mins = max(0, int(time.time() - float(cur.get("since", time.time())))) // 60
                raise Busy(f"BUSY: another AI session ({cur.get('label', 'unknown')}) is using this computer "
                           f"(for {mins} min). Do not fight it over the mouse: wait a minute and try again, or ask "
                           "the user. The user can stop that session from its DeskPilot panel.")
            time.sleep(0.25)

    def release(self) -> None:
        cur = _read()
        if cur and cur.get("pid") == self.pid:
            try:
                LOCK.unlink()
            except OSError:
                pass
        self.held = False
