"""Kill switch: end every DeskPilot server at once, from the overlay process.

Stop asks the agent to end its task; Kill does not ask. It terminates the DeskPilot servers (so no AI has
desktop tools left, even in the middle of an action), closes DeskPilot's own dialogs, lets go of any key or
mouse button the agent was holding, and leaves a flag file. While the flag exists, a restarted server asks
the user before it controls anything again (`deskpilot unlock` clears it too). Apps the agent opened stay open.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import time

from .config import HOME

KILL_FLAG = HOME / "killed"
HOTKEY = "Ctrl+Alt+Backspace"
SERVER_PID_ENV = "DESKPILOT_SERVER_PID"

# Keys and buttons a half-finished action might leave pressed.
_KEYS = (0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C)  # shift ctrl alt, L/R, win
_BUTTONS = ((0x01, 0x0004), (0x02, 0x0010), (0x04, 0x0040))               # vk, MOUSEEVENTF_*UP

# Command lines of DeskPilot's own processes: the server (installed exe, uv launcher, `python -m deskpilot`)
# and its helper windows. Matched on python/deskpilot executables only, so a shell that merely mentions
# "deskpilot" is never touched.
_OURS = re.compile(r"deskpilot(\.exe)?\s+serve\b|deskpilot\.exe\s*$|-m\s+deskpilot(\.server|\.cli)?(\s+serve)?\s*$"
                   r"|-m\s+deskpilot\.(overlay|confirm|picker)\b", re.I)
_EXES = ("python.exe", "pythonw.exe", "deskpilot.exe", "python", "deskpilot")


def write_flag(reason: str) -> None:
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        KILL_FLAG.write_text(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": reason}),
                             encoding="utf-8")
    except OSError:
        pass


def killed() -> bool:
    return KILL_FLAG.exists()


def clear_flag() -> None:
    try:
        KILL_FLAG.unlink()
    except FileNotFoundError:
        pass


def is_deskpilot(name: str, cmdline: list[str]) -> bool:
    return name.lower() in _EXES and bool(_OURS.search(" ".join(cmdline)))


def victims(server_pid: int | None, me: int) -> list:
    """Every DeskPilot server on this PC (one per AI session; the user can't know which one misbehaves) and their
    helper processes (dialogs, picker, other overlays), plus `server_pid`'s tree. Never this overlay or the
    launcher it runs under, and never apps the agent opened."""
    import psutil

    spare = {me}
    try:
        for p in psutil.Process(me).parents():
            if "deskpilot.overlay" in " ".join(p.cmdline()):
                spare.add(p.pid)
    except psutil.Error:
        pass
    found: dict[int, psutil.Process] = {}
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if p.pid not in spare and is_deskpilot(p.info["name"] or "", p.info["cmdline"] or []):
                found[p.pid] = p
        except psutil.Error:
            pass
    if server_pid and server_pid not in spare:
        try:
            srv = psutil.Process(server_pid)
            found[srv.pid] = srv
            for k in srv.children(recursive=True):
                if k.pid not in spare and "python" in k.name().lower():
                    found[k.pid] = k
        except psutil.Error:
            pass
    return list(found.values())


def release_input() -> None:
    """Let go of every modifier key and mouse button that is still down."""
    user32 = ctypes.windll.user32
    for vk in _KEYS:
        if user32.GetAsyncKeyState(vk) & 0x8000:
            user32.keybd_event(vk, 0, 0x2, 0)               # KEYEVENTF_KEYUP
    for vk, up in _BUTTONS:
        if user32.GetAsyncKeyState(vk) & 0x8000:
            user32.mouse_event(up, 0, 0, 0, 0)


def kill(server_pid: int | None, reason: str = "kill switch") -> int:
    """Terminate every DeskPilot server and helper, release input, log it. Returns how many processes ended."""
    write_flag(reason)
    n = 0
    for p in victims(server_pid, os.getpid()):
        try:
            p.kill()  # TerminateProcess: no cleanup code in the server gets to run
            n += 1
        except Exception:
            pass
    release_input()
    try:
        from .timeline import append

        append({"pid": os.getpid(), "kind": "kill", "ok": False,
                "text": f"KILL SWITCH ({reason}): {n} DeskPilot processes ended"})
    except Exception:
        pass
    try:
        from .policy import audit

        audit({"event": "kill", "reason": reason, "processes": n})
    except Exception:
        pass
    return n
