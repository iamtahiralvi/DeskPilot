"""Admin windows (UIPI) and the optional, on-demand admin helper.

Windows gives every process an integrity level. DeskPilot runs at the user's normal (medium) level, so Windows
silently drops its clicks and keystrokes into windows of programs that run as administrator (high level: Task
Manager, regedit, installers, tools such as OCCT or HWiNFO) and hides their elements from it. Screenshots and OCR
still work. This is User Interface Privilege Isolation (UIPI).

This module:
- tells whether a window belongs to a process above DeskPilot's level, so DeskPilot can say so clearly instead of
  clicking blindly (a dropped click can land on the window behind);
- runs the admin helper when the user allows it: a small DeskPilot process started with a UAC prompt. While it
  runs, DeskPilot's mouse and keyboard input, window commands and accessibility reads go through it, so they reach
  admin windows too. Apps DeskPilot launches still start at the normal level.

The helper only talks to the DeskPilot server that started it, over a named pipe that only one client can open:
the server checks the pipe client is the process it started, and the helper checks the pipe server is the process
named on its command line. It exits (and lets go of any held key or button) as soon as that server goes away.
UAC prompts themselves are on Windows' secure desktop: nothing, not even the helper, can click them.
"""
from __future__ import annotations

import ctypes
import json
import os
import secrets
import struct
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import asdict

IS_WINDOWS = sys.platform == "win32"

# Integrity level RIDs.
LOW, MEDIUM, HIGH, SYSTEM = 0x1000, 0x2000, 0x3000, 0x4000
LEVEL_NAMES = {LOW: "low", MEDIUM: "normal", HIGH: "administrator", SYSTEM: "system"}

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_INTEGRITY_LEVEL = 25
ERROR_ACCESS_DENIED = 5
ERROR_CANCELLED = 1223

if IS_WINDOWS:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _u32 = ctypes.WinDLL("user32", use_last_error=True)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD)]
    _adv.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    _adv.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    _adv.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    _adv.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    _u32.OpenInputDesktop.restype = wintypes.HANDLE
    _u32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _u32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                               ctypes.POINTER(wintypes.DWORD)]
    _u32.CloseDesktop.argtypes = [wintypes.HANDLE]


def _token_level(token) -> int | None:
    size = wintypes.DWORD(0)
    _adv.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size))
    if not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if not _adv.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, buf, size, ctypes.byref(size)):
        return None
    sid = ctypes.c_void_p.from_buffer(buf).value  # TOKEN_MANDATORY_LABEL.Label.Sid
    count = _adv.GetSidSubAuthorityCount(sid).contents.value
    return int(_adv.GetSidSubAuthority(sid, count - 1).contents.value)


def own_level() -> int:
    if not IS_WINDOWS:
        return MEDIUM
    token = wintypes.HANDLE()
    if not _adv.OpenProcessToken(_k32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        return MEDIUM
    try:
        return _token_level(token) or MEDIUM
    finally:
        _k32.CloseHandle(token)


def process_level(pid: int) -> int | None:
    """Integrity level of a process. HIGH when Windows won't even let us look (that is what admin and system
    processes do to a normal-level caller); None when it's unknown for another reason (e.g. it has exited)."""
    if not IS_WINDOWS or not pid:
        return None
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return HIGH if ctypes.get_last_error() == ERROR_ACCESS_DENIED else None
    try:
        token = wintypes.HANDLE()
        if not _adv.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(token)):
            return HIGH if ctypes.get_last_error() == ERROR_ACCESS_DENIED else None
        try:
            return _token_level(token)
        finally:
            _k32.CloseHandle(token)
    finally:
        _k32.CloseHandle(h)


_OWN: int | None = None
_cache: dict[int, tuple[float, bool]] = {}


def is_above_us(pid: int) -> bool:
    """True when `pid` runs at a higher integrity level than this process, so Windows blocks our input to it."""
    global _OWN
    if _OWN is None:
        _OWN = own_level()
    hit = _cache.get(pid)
    now = time.time()
    if hit and now - hit[0] < 30:
        return hit[1]
    lvl = process_level(pid)
    above = lvl is not None and lvl > _OWN
    _cache[pid] = (now, above)
    return above


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    _u32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return pid.value


def window_above_us(hwnd: int) -> bool:
    if not IS_WINDOWS or not hwnd:
        return False
    root = _u32.GetAncestor(wintypes.HWND(hwnd), 2) or hwnd  # GA_ROOT
    return is_above_us(window_pid(root))


SECURE_DESKTOP_MSG = ("WINDOWS SECURE DESKTOP: a UAC prompt (\"Do you want to allow this app to make changes?\"), or "
                      "the Ctrl+Alt+Del / sign-in screen, is showing. Nothing but the user can see or answer it, not "
                      "even admin mode; screenshots meanwhile show black or a frozen picture. Stop and ask the user to "
                      "answer it, then look again.")


def secure_desktop() -> bool:
    """True while Windows shows its secure desktop (UAC prompt, Ctrl+Alt+Del, credential UI). Normal processes
    can't open that input desktop at all; otherwise its name tells (the user's desktop is "Default")."""
    if not IS_WINDOWS:
        return False
    h = _u32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
    if not h:
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        buf = ctypes.create_unicode_buffer(256)
        need = wintypes.DWORD(0)
        if not _u32.GetUserObjectInformationW(h, 2, buf, ctypes.sizeof(buf), ctypes.byref(need)):  # UOI_NAME
            return False
        return buf.value.lower() != "default"
    finally:
        _u32.CloseDesktop(h)


_needs_cache: dict[str, str | None] = {}


def needs_admin(path: str | None) -> str | None:
    """'requireAdministrator' or 'highestAvailable' when an .exe's manifest asks Windows for admin rights (so
    starting it shows a UAC prompt for an admin user), else None."""
    if not IS_WINDOWS or not path or not path.lower().endswith(".exe") or not os.path.isfile(path):
        return None
    key = os.path.normcase(os.path.abspath(path))
    if key in _needs_cache:
        return _needs_cache[key]
    level = None
    try:
        import win32api

        h = win32api.LoadLibraryEx(key, 0, 0x00000002 | 0x00000020)  # AS_DATAFILE | AS_IMAGE_RESOURCE
        try:
            for rid in (1, 2):  # 1 = exe manifest, 2 = isolation-aware dll manifest
                try:
                    text = win32api.LoadResource(h, 24, rid).decode("utf-8", "ignore")  # RT_MANIFEST
                except Exception:
                    continue
                for want in ("requireAdministrator", "highestAvailable"):
                    if f'level="{want}"' in text or f"level='{want}'" in text:
                        level = want
                break
        finally:
            win32api.FreeLibrary(h)
    except Exception:
        level = None
    _needs_cache[key] = level
    return level


def app_id_path(app_id: str) -> str | None:
    """The .exe behind a Start-menu AppID when it is a plain desktop app: a full path, or a known-folder GUID
    plus a relative path ("{6D809377-...}\\OCCT\\OCCT.exe"). None for Store apps and anything else."""
    if not app_id or not app_id.lower().endswith(".exe"):
        return None
    if os.path.isabs(app_id):
        return app_id
    if app_id.startswith("{") and "}" in app_id:
        guid, rest = app_id[:app_id.index("}") + 1], app_id[app_id.index("}") + 1:].lstrip("\\")
        try:
            from win32com.shell import shell

            return os.path.join(shell.SHGetKnownFolderPath(guid, 0, None), rest)
        except Exception:
            return None
    return None


def launch_admin_note(name: str, level: str) -> str:
    asks = "always asks" if level == "requireAdministrator" else "asks (for administrator accounts)"
    return (f"NOTE: {name} {asks} for administrator rights: Windows shows a UAC prompt that only the user can answer. "
            "Don't click or type until they have; its window will run as administrator (see screen_info(op=\"admin\") "
            "to act in it).")


def cursor_blocked_message(title: str, exe: str, helper_allowed: bool) -> str:
    who = f'"{title[:60]}"' + (f" ({exe})" if exe else "")
    msg = (f"ADMIN WINDOW IN FRONT: {who} runs as administrator and is the active window. While it is, Windows "
           "blocks DeskPilot from moving the mouse at all, even over other windows, so this step was NOT done. ")
    if helper_allowed:
        msg += ("Bring the window you want to the front first with window(\"focus\", \"wN\"), ask the user to do "
                "the step, or call screen_info(op=\"admin\") to ask them to turn on admin mode.")
    else:
        msg += "Bring the window you want to the front first with window(\"focus\", \"wN\"), or ask the user."
    return msg


def blocked_message(title: str, exe: str, helper_allowed: bool) -> str:
    who = f'"{title[:60]}"' + (f" ({exe})" if exe else "")
    msg = (f"ADMIN WINDOW: {who} runs as administrator. Windows blocks DeskPilot's clicks and typing into it (UIPI), "
           "so this step was NOT done (it would have landed on whatever is behind it). Screenshots and OCR of it "
           "still work. ")
    if helper_allowed:
        msg += ("Ask the user to do this step themselves, or call screen_info(op=\"admin\") to ask them to turn on "
                "admin mode (they approve a Windows UAC prompt), then try again.")
    else:
        msg += "Admin mode is switched off in the user's DeskPilot policy, so ask the user to do this step."
    return msg


# ---------------------------------------------------------------- framing (4-byte length + JSON)
def _send_msg(h, obj) -> None:
    import win32file

    data = json.dumps(obj, separators=(",", ":"), default=str).encode("utf-8")
    win32file.WriteFile(h, struct.pack("<I", len(data)) + data)


def _read_exact(h, n: int) -> bytes:
    import win32file

    out = b""
    while len(out) < n:
        _, chunk = win32file.ReadFile(h, n - len(out))
        if not chunk:
            raise EOFError("pipe closed")
        out += chunk
    return out


def _recv_msg(h):
    (n,) = struct.unpack("<I", _read_exact(h, 4))
    if n > 64 * 1024 * 1024:
        raise ValueError("message too large")
    return json.loads(_read_exact(h, n).decode("utf-8"))


# ---------------------------------------------------------------- Node <-> dict
def node_to_dict(n) -> dict | None:
    return None if n is None else asdict(n)


def node_from_dict(d):
    if d is None:
        return None
    from .perceive import Node

    kids = [node_from_dict(k) for k in d.pop("children", [])]
    d["rect"] = tuple(d.get("rect") or (0, 0, 0, 0))
    n = Node(**d)
    n.children = kids
    return n


# ---------------------------------------------------------------- server side (the normal DeskPilot process)
class HelperError(Exception):
    pass


def _helper_command() -> tuple[str, str]:
    """(program, arguments prefix) that runs `deskpilot.cli` with the same Python as this server, windowless."""
    exe = sys.executable
    w = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.basename(exe).lower() == "python.exe" and os.path.exists(w):
        exe = w
    return exe, "-I -m deskpilot.cli admin-helper"


class AdminHelper:
    """The DeskPilot server's handle on the admin helper. All calls are thread-safe."""

    def __init__(self):
        self.pipe = None
        self.proc = None  # process handle from ShellExecuteEx
        self.pid = 0
        self.lock = threading.Lock()
        self.started_at = 0.0

    @property
    def active(self) -> bool:
        return self.pipe is not None

    def start(self, elevate: bool = True, timeout: float = 30.0) -> None:
        """Start the helper. With elevate=True Windows shows a UAC prompt; this blocks until the user answers.
        Raises HelperError when the user declines or the helper doesn't connect."""
        import pywintypes
        import win32api
        import win32con
        import win32event
        import win32file
        import win32pipe

        if self.active:
            return
        name = rf"\\.\pipe\deskpilot-admin-{os.getpid()}-{secrets.token_hex(8)}"
        FIRST_INSTANCE, REJECT_REMOTE = 0x00080000, 0x00000008
        pipe = win32pipe.CreateNamedPipe(
            name, win32pipe.PIPE_ACCESS_DUPLEX | FIRST_INSTANCE,
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT | REJECT_REMOTE,
            1, 1 << 20, 1 << 20, 0, None)
        exe, args = _helper_command()
        params = f'{args} "{name}" {os.getpid()}'
        try:
            if elevate:
                from win32com.shell import shell, shellcon

                try:
                    info = shell.ShellExecuteEx(fMask=shellcon.SEE_MASK_NOCLOSEPROCESS | 0x00000400,  # NO_UI errors
                                                lpVerb="runas", lpFile=exe, lpParameters=params,
                                                nShow=win32con.SW_HIDE)
                except pywintypes.error as e:
                    if e.winerror == ERROR_CANCELLED:
                        raise HelperError("the user declined the Windows UAC prompt, so admin mode is off")
                    raise HelperError(f"could not start the admin helper: {e.strerror}")
                self.proc = info["hProcess"]
                self.pid = win32process_id(self.proc)
            else:  # tests: same protocol, no elevation
                import subprocess

                p = subprocess.Popen(f'"{exe}" {params}', creationflags=0x08000000)
                self.proc = win32api.OpenProcess(0x1000 | 0x00100000, False, p.pid)  # QUERY_LIMITED | SYNCHRONIZE
                self.pid = p.pid

            connected = threading.Event()
            err: list = []

            def accept():
                try:
                    win32pipe.ConnectNamedPipe(pipe, None)
                except pywintypes.error as e:
                    if e.winerror != 535:  # ERROR_PIPE_CONNECTED: the client was quicker than us, fine
                        err.append(e)
                connected.set()

            threading.Thread(target=accept, daemon=True).start()
            end = time.time() + timeout
            while not connected.wait(0.2):
                if win32event.WaitForSingleObject(self.proc, 0) == win32event.WAIT_OBJECT_0:
                    raise HelperError("the admin helper exited before it connected")
                if time.time() > end:
                    raise HelperError("the admin helper did not connect in time")
            if err:
                raise HelperError(f"admin helper connection failed: {err[0]}")
            client = win32pipe.GetNamedPipeClientProcessId(pipe)
            if not _is_self_or_child(client, self.pid):
                raise HelperError(f"unexpected process {client} on the admin pipe; refused")
            self.pipe = pipe
            pipe = None
            self.started_at = time.time()
            if self.call("ping") != "pong":
                raise HelperError("the admin helper did not answer")
        except Exception:
            self.stop()
            raise
        finally:
            if pipe is not None:
                try:
                    win32file.CloseHandle(pipe)
                except Exception:
                    pass

    def call(self, op: str, **args):
        with self.lock:
            if self.pipe is None:
                raise HelperError("admin helper is not running")
            try:
                _send_msg(self.pipe, {"op": op, "args": args})
                res = _recv_msg(self.pipe)
            except Exception as e:
                self._drop()
                raise HelperError(f"admin helper stopped ({e.__class__.__name__}); admin mode is off")
        if "error" in res:
            raise HelperError(res["error"])
        return res.get("ok")

    def _drop(self) -> None:
        import win32file

        p, self.pipe = self.pipe, None
        if p is not None:
            try:
                win32file.CloseHandle(p)
            except Exception:
                pass

    def stop(self) -> None:
        """Ask the helper to quit (it also quits on its own when the pipe closes)."""
        if self.pipe is not None:
            try:
                with self.lock:
                    _send_msg(self.pipe, {"op": "quit", "args": {}})
            except Exception:
                pass
        with self.lock:
            self._drop()
        if self.proc is not None:
            try:
                import win32event

                win32event.WaitForSingleObject(self.proc, 2000)
                self.proc.Close()
            except Exception:
                pass
            self.proc = None
        self.pid = 0


class RoutedUIA:
    """Wraps the server's UIA reader. While the admin helper runs, reads that may involve an admin window are
    done by the helper (a normal-level process can't see inside admin windows); everything else stays local."""

    def __init__(self, local, helper: AdminHelper):
        self.local = local
        self.helper = helper

    def __getattr__(self, name):
        return getattr(self.local, name)

    def _remote(self, op: str, fallback, **args):
        if self.helper.active:
            try:
                return self.helper.call(op, **args)
            except HelperError:
                pass
        return fallback()

    def tree(self, hwnd: int):
        if self.helper.active and window_above_us(hwnd):
            try:
                return node_from_dict(self.helper.call("tree", hwnd=hwnd))
            except HelperError:
                pass
        return self.local.tree(hwnd)

    def focused(self):
        if self.helper.active:
            try:
                return node_from_dict(self.helper.call("focused"))
            except HelperError:
                pass
        return self.local.focused()

    def at_point(self, x: int, y: int):
        if self.helper.active:
            try:
                return node_from_dict(self.helper.call("at_point", x=x, y=y))
            except HelperError:
                pass
        return self.local.at_point(x, y)

    def relocate(self, x: int, y: int, role: str, name: str, old_rect):
        r = self._remote("relocate", lambda: self.local.relocate(x, y, role, name, old_rect),
                         x=x, y=y, role=role, name=name, rect=list(old_rect))
        return tuple(r) if r else None

    def address_bar_info(self, hwnd: int):
        r = self._remote("address_bar_info", lambda: self.local.address_bar_info(hwnd), hwnd=hwnd)
        return (tuple(r[0]), r[1]) if r else None

    def address_bar(self, hwnd: int):
        info = self.address_bar_info(hwnd)
        return (info[1] or None) if info else None


def _is_self_or_child(pid: int, started: int) -> bool:
    """The process we started, or the real Python it runs (a venv's python.exe is a launcher that starts the base
    interpreter as its child)."""
    if pid == started:
        return True
    try:
        import psutil

        return psutil.Process(pid).ppid() == started
    except Exception:
        return False


def win32process_id(handle) -> int:
    return int(_k32.GetProcessId(wintypes.HANDLE(int(handle))))


# ---------------------------------------------------------------- the helper process
def _release_held() -> None:
    from .killswitch import release_input

    try:
        release_input()
    except Exception:
        pass


def _do_input(items: list) -> None:
    from . import act

    inputs = []
    for it in items:
        if it[0] == act.INPUT_MOUSE:
            inputs.append(act._mouse(int(it[1]), int(it[2])))
        else:
            inputs.append(act._key(int(it[1]), int(it[2]), int(it[3])))
    act._send_local(*inputs)


def serve_helper(pipe_name: str, server_pid: int) -> int:
    """Main loop of the admin helper process (`deskpilot admin-helper <pipe> <server pid>`)."""
    import win32file
    import win32pipe

    from . import winutil
    from .config import set_dpi_awareness
    from .perceive import UIA

    set_dpi_awareness()  # same pixel coordinates as the server
    try:
        h = win32file.CreateFile(pipe_name, win32file.GENERIC_READ | win32file.GENERIC_WRITE, 0, None,
                                 win32file.OPEN_EXISTING, 0, None)
    except Exception:
        return 2
    try:
        if win32pipe.GetNamedPipeServerProcessId(h) != server_pid:
            return 3  # not the DeskPilot server that asked for us: refuse to serve anyone else
        uia = UIA()
        while True:
            try:
                req = _recv_msg(h)
            except Exception:
                break
            op, a = req.get("op"), req.get("args") or {}
            if op == "quit":
                break
            try:
                if op == "ping":
                    res = "pong"
                elif op == "input":
                    _do_input(a["items"])
                    res = True
                elif op == "cursor":
                    from .act import user32 as _u

                    res = bool(_u.SetCursorPos(int(a["x"]), int(a["y"])))
                elif op == "focus":
                    res = winutil.focus(int(a["hwnd"]), local=True)
                elif op == "close":
                    winutil.close(int(a["hwnd"]), local=True)
                    res = True
                elif op == "set_state":
                    winutil.set_state(int(a["hwnd"]), a["op"], local=True)
                    res = True
                elif op == "move_to":
                    from .config import Rect

                    winutil.move_to(int(a["hwnd"]), Rect(*a["area"]), bool(a.get("maximize", True)), local=True)
                    res = True
                elif op == "tree":
                    res = node_to_dict(uia.tree(int(a["hwnd"])))
                elif op == "focused":
                    res = node_to_dict(uia.focused())
                elif op == "at_point":
                    res = node_to_dict(uia.at_point(int(a["x"]), int(a["y"])))
                elif op == "relocate":
                    res = uia.relocate(int(a["x"]), int(a["y"]), a["role"], a["name"], tuple(a["rect"]))
                elif op == "address_bar_info":
                    res = uia.address_bar_info(int(a["hwnd"]))
                else:
                    raise ValueError(f"unknown op {op}")
                out = {"ok": res}
            except Exception as e:
                out = {"error": f"{e.__class__.__name__}: {e}"}
            try:
                _send_msg(h, out)
            except Exception:
                break
    finally:
        _release_held()
        try:
            win32file.CloseHandle(h)
        except Exception:
            pass
    return 0
