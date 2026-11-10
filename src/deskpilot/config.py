"""Paths, session state, monitor geometry and DPI setup."""
from __future__ import annotations

import ctypes
import json
import os
from dataclasses import dataclass
from pathlib import Path

HOME = Path(os.environ.get("DESKPILOT_HOME", Path.home() / ".deskpilot"))
SESSION_FILE = HOME / "session.json"
POLICY_FILE = HOME / "policy.yaml"
AUDIT_FILE = HOME / "audit.jsonl"


def set_dpi_awareness() -> None:
    """Per-monitor DPI aware v2 so every coordinate is a physical pixel."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def contains(self, x: float, y: float) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def intersects(self, l: int, t: int, r: int, b: int) -> bool:
        return l < self.right and r > self.left and t < self.bottom and b > self.top


def monitors() -> list[Rect]:
    """Physical monitors, 1-based order as Windows/mss reports them (index 0 here = monitor 1)."""
    import mss

    with mss.MSS() as s:
        return [Rect(m["left"], m["top"], m["width"], m["height"]) for m in s.monitors[1:]]


def primary_index() -> int:
    for i, m in enumerate(monitors()):
        if m.left == 0 and m.top == 0:
            return i
    return 0


@dataclass
class Session:
    monitor: int | str = 0          # index into monitors(), or "all"
    mode: str = "normal"            # normal | strict
    remember: bool = False          # skip the picker next time
    picked: bool = False            # picked during this server run

    def areas(self) -> list[Rect]:
        mons = monitors()
        if self.monitor == "all":
            return mons
        idx = int(self.monitor)
        return [mons[idx if idx < len(mons) else 0]]

    def primary_area(self) -> Rect:
        return self.areas()[0]

    def allowed(self, x: float, y: float) -> bool:
        return any(a.contains(x, y) for a in self.areas())

    def save(self) -> None:
        HOME.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(json.dumps({"monitor": self.monitor, "mode": self.mode, "remember": self.remember}))

    @classmethod
    def load(cls) -> "Session":
        try:
            d = json.loads(SESSION_FILE.read_text())
            s = cls(monitor=d.get("monitor", 0), mode=d.get("mode", "normal"), remember=d.get("remember", False))
            s.picked = s.remember
            return s
        except Exception:
            return cls()
