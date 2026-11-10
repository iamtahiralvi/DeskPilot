"""Real mouse + keyboard input through SendInput, with smooth visible movement."""
from __future__ import annotations

import ctypes
import math
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x1, 0x2, 0x4
MOUSE_FLAGS = {
    "left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040),
}
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _send(*inputs: INPUT) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _mouse(flags: int, data: int = 0) -> INPUT:
    i = INPUT(type=INPUT_MOUSE)
    i.u.mi = MOUSEINPUT(0, 0, ctypes.c_ulong(data & 0xFFFFFFFF).value, flags, 0, 0)
    return i


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    i = INPUT(type=INPUT_KEYBOARD)
    i.u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return i


class UserInterrupted(Exception):
    """The human moved the mouse while the agent was moving it."""


def cursor_pos() -> tuple[int, int]:
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


class Input:
    def __init__(self, on_move=None, on_event=None, speed: float = 1.0):
        self.on_move = on_move or (lambda x, y: None)
        self.on_event = on_event or (lambda kind, **kw: None)
        self.speed = speed
        self.grace_until = 0.0  # ignore human mouse motion until then (e.g. just after they answered a dialog)

    def grace(self, seconds: float = 1.2) -> None:
        self.grace_until = time.time() + seconds

    # ---------- mouse ----------
    def move(self, x: int, y: int) -> None:
        sx, sy = cursor_pos()
        dist = math.hypot(x - sx, y - sy)
        steps = max(1, min(40, int(dist / 25)))
        duration = min(0.45, 0.08 + dist / 4000) / max(self.speed, 0.1)
        last = (sx, sy)
        for i in range(1, steps + 1):
            cx, cy = cursor_pos()
            if math.hypot(cx - last[0], cy - last[1]) > 30 and time.time() > self.grace_until:
                raise UserInterrupted("You moved the mouse, so the agent stopped. Ask it to continue when ready.")
            t = i / steps
            e = 1 - (1 - t) ** 3  # ease-out
            nx, ny = round(sx + (x - sx) * e), round(sy + (y - sy) * e)
            user32.SetCursorPos(nx, ny)
            self.on_move(nx, ny)
            last = (nx, ny)
            time.sleep(duration / steps)

    def click(self, x: int, y: int, button: str = "left", count: int = 1) -> None:
        self.move(x, y)
        down, up = MOUSE_FLAGS[button]
        self.on_event("click", x=x, y=y, button=button)
        for _ in range(count):
            _send(_mouse(down), _mouse(up))
            time.sleep(0.05)

    def mouse_down(self, x: int, y: int, button: str = "left") -> None:
        self.move(x, y)
        _send(_mouse(MOUSE_FLAGS[button][0]))

    def mouse_up(self, x: int, y: int, button: str = "left") -> None:
        self.move(x, y)
        _send(_mouse(MOUSE_FLAGS[button][1]))

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left") -> None:
        self.move(x1, y1)
        _send(_mouse(MOUSE_FLAGS[button][0]))
        time.sleep(0.12)
        # small nudge so apps register a drag start
        user32.SetCursorPos(x1 + 6, y1 + 6)
        time.sleep(0.08)
        try:
            self.move(x2, y2)
            time.sleep(0.12)
        finally:
            _send(_mouse(MOUSE_FLAGS[button][1]))
        self.on_event("click", x=x2, y=y2, button=button)

    def scroll(self, x: int, y: int, dy: int = 0, dx: int = 0) -> None:
        """dy>0 scrolls down (in 'notches'), dx>0 scrolls right."""
        self.move(x, y)
        for _ in range(abs(dy)):
            _send(_mouse(MOUSEEVENTF_WHEEL, -120 if dy > 0 else 120))
            time.sleep(0.03)
        for _ in range(abs(dx)):
            _send(_mouse(MOUSEEVENTF_HWHEEL, 120 if dx > 0 else -120))
            time.sleep(0.03)

    # ---------- keyboard ----------
    def type_text(self, text: str, delay: float = 0.05) -> None:
        """Types like a fast human. Key down and key up go as separate events with a short gap: sending them
        together at 4 ms/char made apps such as the Win11 Notepad drop characters."""
        self.on_event("label", text=f"typing: {text[:30]}{'...' if len(text) > 30 else ''}")
        time.sleep(0.06)  # let focus settle after a click / launch before the first key
        for ch in text:
            if ch == "\n":
                self.tap("enter")
                continue
            if ch == "\t":
                self.tap("tab")
                continue
            data = ch.encode("utf-16-le")
            units = [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]
            downs = [_key(0, u, KEYEVENTF_UNICODE) for u in units]
            ups = [_key(0, u, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP) for u in units]
            _send(*downs)
            time.sleep(0.002)
            _send(*ups)
            time.sleep(delay)

    def press(self, combo: str) -> None:
        """'ctrl+shift+s', 'enter', 'win+e', 'f5'."""
        keys = [vk_for(k) for k in combo.replace(" ", "").split("+") if k]
        self.on_event("label", text=combo)
        _send(*[_key(vk, 0, _ext(vk)) for vk in keys])
        time.sleep(0.02)
        _send(*[_key(vk, 0, _ext(vk) | KEYEVENTF_KEYUP) for vk in reversed(keys)])
        time.sleep(0.03)

    def tap(self, key: str) -> None:
        self.press(key)

    def key_down(self, key: str) -> None:
        vk = vk_for(key)
        _send(_key(vk, 0, _ext(vk)))

    def key_up(self, key: str) -> None:
        vk = vk_for(key)
        _send(_key(vk, 0, _ext(vk) | KEYEVENTF_KEYUP))


_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x6F, 0xA3, 0xA5}


def _ext(vk: int) -> int:
    return KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0


VK = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "windows": 0x5B, "cmd": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "capslock": 0x14, "printscreen": 0x2C, "apps": 0x5D, "menu": 0x5D,
    "volumeup": 0xAF, "volumedown": 0xAE, "volumemute": 0xAD, "playpause": 0xB3,
    "plus": 0xBB, "minus": 0xBD, "comma": 0xBC, "period": 0xBE,
}
for _i in range(1, 25):
    VK[f"f{_i}"] = 0x6F + _i


def vk_for(key: str) -> int:
    k = key.lower()
    if k in VK:
        return VK[k]
    if len(k) == 1:
        r = user32.VkKeyScanW(ord(k))
        if r != -1:
            return r & 0xFF
    raise ValueError(f"unknown key '{key}'")
