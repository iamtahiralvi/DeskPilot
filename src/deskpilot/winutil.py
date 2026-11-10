"""Top-level window helpers (enumerate, focus, move, close)."""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass

import win32con
import win32gui
import win32process

from .config import Rect

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi
OWN_PIDS: set[int] = {os.getpid()}
SKIP_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW", "Windows.UI.Core.CoreWindow",
                "DeskPilotOverlay", "TkTopLevel_DeskPilotOverlay"}


@dataclass
class Win:
    hwnd: int
    title: str
    cls: str
    rect: tuple[int, int, int, int]
    minimized: bool
    pid: int

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.rect
        return (l + r) // 2, (t + b) // 2

    def exe(self) -> str:
        try:
            import psutil  # optional
            return psutil.Process(self.pid).name()
        except Exception:
            return _exe_name(self.pid)


def _exe_name(pid: int) -> str:
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def _cloaked(hwnd: int) -> bool:
    v = ctypes.c_int(0)
    dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 14, ctypes.byref(v), ctypes.sizeof(v))
    return v.value != 0


def windows(include_minimized: bool = False) -> list[Win]:
    """Visible app windows in z-order (topmost first)."""
    out: list[Win] = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        cls = win32gui.GetClassName(hwnd)
        if not title or cls in SKIP_CLASSES:
            return True
        ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if ex & win32con.WS_EX_TOOLWINDOW and not ex & win32con.WS_EX_APPWINDOW:
            return True
        if _cloaked(hwnd):
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid in OWN_PIDS:
            return True
        mini = bool(win32gui.IsIconic(hwnd))
        if mini and not include_minimized:
            return True
        out.append(Win(hwnd, title, cls, win32gui.GetWindowRect(hwnd), mini, pid))
        return True

    win32gui.EnumWindows(cb, None)
    return out


def on_area(w: Win, areas: list[Rect]) -> bool:
    cx, cy = w.center
    return any(a.contains(cx, cy) for a in areas)


def top_window(areas: list[Rect]) -> Win | None:
    for w in windows():
        if on_area(w, areas):
            return w
    return None


def foreground() -> int:
    return user32.GetForegroundWindow()


def focus(hwnd: int) -> bool:
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    # Never tap Alt here: a lone Alt press opens the menu bar (Notepad, Explorer, Office...) and the next
    # typed characters go into the menu instead of the document.
    def _try() -> bool:
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        return foreground() == hwnd

    ok = _try()
    if not ok:
        # Join the current foreground thread's input queue so Windows lets us hand focus over.
        k32 = ctypes.windll.kernel32
        me = k32.GetCurrentThreadId()
        fg_thread = user32.GetWindowThreadProcessId(foreground(), None)
        attached = fg_thread and fg_thread != me and user32.AttachThreadInput(me, fg_thread, True)
        try:
            user32.BringWindowToTop(hwnd)
            ok = _try()
        finally:
            if attached:
                user32.AttachThreadInput(me, fg_thread, False)
    if not ok:
        # Last resort: a no-op key event (VK 0xFF, no meaning) also lifts the foreground lock.
        user32.keybd_event(0xFF, 0, 0, 0)
        user32.keybd_event(0xFF, 0, 2, 0)
        ok = _try()
    time.sleep(0.15)
    return foreground() == hwnd


def move_to(hwnd: int, area: Rect, maximize: bool = True) -> None:
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = min(r - l, area.width), min(b - t, area.height)
    x, y = area.left + (area.width - w) // 2, area.top + (area.height - h) // 2
    win32gui.SetWindowPos(hwnd, 0, x, y, w, h, win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
    if maximize:
        win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)


def set_state(hwnd: int, op: str) -> None:
    cmd = {"minimize": win32con.SW_MINIMIZE, "maximize": win32con.SW_MAXIMIZE,
           "restore": win32con.SW_RESTORE}[op]
    win32gui.ShowWindow(hwnd, cmd)


def close(hwnd: int) -> None:
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
