"""The Desktop controller: everything the MCP tools call. All methods run on one worker thread (COM/UIA)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import win32gui

from . import act as act_mod
from . import clip, elevation, killswitch
from . import routines as R
from . import timeline as TL
from .screenlock import Busy, ScreenLock
from . import winutil
from .act import Input, UserBusy, UserInterrupted
from .browser import BROWSER_EXES, Browsers, launch_agent_browser
from .config import Rect, Session, monitors
from .perceive import (EDITABLE, UIA, RefTable, changed, describe, diff, diff_box, draw_marks, encode_jpeg,
                       find_nodes, format_tree, grab, is_black, moving_box, norm_text, ocr, ocr_match, thumb, _clip)
from .policy import Gate, audit, normalize_combo
from .privacy import Privacy, black_out

NO_WINDOW = 0x08000000
WEB_ROLE = {"clickable": "button", "combobox": "combo", "textbox": "edit", "searchbox": "edit", "menuitemcheckbox":
            "menuitem", "menuitemradio": "menuitem", "switch": "checkbox", "option": "item", "treeitem": "treeitem"}


class AgentError(Exception):
    pass


class OverlayClient:
    """Talks to the overlay + control panel process."""

    def __init__(self, on_cmd):
        self.proc = None
        self.on_cmd = on_cmd
        self.lock = threading.Lock()

    def start(self, areas: list[Rect], mode: str):
        if self.proc is None or self.proc.poll() is not None:
            try:
                env = {**os.environ, killswitch.SERVER_PID_ENV: str(os.getpid())}  # the kill switch's target
                self.proc = subprocess.Popen([sys.executable, "-m", "deskpilot.overlay"], stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, text=True, bufsize=1, creationflags=NO_WINDOW,
                                             env=env)
                winutil.OWN_PIDS.add(self.proc.pid)
                threading.Thread(target=self._read, daemon=True).start()
            except Exception:
                self.proc = None
                return
        self.send({"op": "areas", "rects": [[a.left, a.top, a.width, a.height] for a in areas], "mode": mode})

    def _read(self):
        proc = self.proc
        for line in proc.stdout:
            if line.strip():
                self.on_cmd(line.strip())

    def send(self, msg: dict):
        p = self.proc
        if p is None or p.poll() is not None:
            return
        try:
            with self.lock:
                p.stdin.write(json.dumps(msg) + "\n")
                p.stdin.flush()
        except Exception:
            pass

    def hide(self):
        self.send({"op": "hide"})

    def stop(self):
        self.send({"op": "quit"})


PAUSE_MSG = {
    "paused": "PAUSED: the user paused you (Ctrl+Alt+Esc or the panel). Stop and wait until they resume.",
    "takeover": "PAUSED: the user took over the computer. Stop and wait until they hand control back.",
    "stopped": "STOPPED by the user. End this task now and tell the user what you completed.",
    "moved": "PAUSED: the user moved the mouse. Stop; they resume from the panel or with Ctrl+Alt+Esc.",
    "budget": "PAUSED: this task reached the token budget the user set. Stop, tell the user what you have done "
              "so far and that they can resume from the DeskPilot panel if they want you to continue.",
}


class Desktop:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="desk", initializer=self._init_thread)
        self.session = Session.load()
        self.gate = Gate()
        self._dialog = self.gate._confirm
        self.gate._confirm = self._confirm_with_ui
        self.privacy = Privacy(self.gate.policy.rules)
        self.refs = RefTable("e")
        self.web: dict[str, dict] = {}
        self.browsers = Browsers()
        self.browsers.allow_real_profile = bool(self.gate.policy.rules.get("use_my_browser", False))
        self.win_refs: dict[int, str] = {}
        self.win_by_ref: dict[str, int] = {}
        self.last: dict = {"hwnd": None, "by_ref": {}}
        self.shot = None  # (left, top, scale) of the last screenshot
        self.pause_reason = ""
        # When the agent last looked (snapshot, find, screenshot of any size). An act() with no look in the last
        # look_fresh_seconds shows the screen instead of acting blind. 0 = must look (start, or after a pause).
        self.last_look = 0.0
        try:
            self.look_fresh = float(self.gate.policy.rules.get("look_fresh_seconds", 60))
        except (TypeError, ValueError):
            self.look_fresh = 60.0
        self.task_log: list[str] = []  # steps of the current task, returned by done()
        self._moving = None       # thumb box of a screen area that never stops changing (video), ignored
        self._last_thumb = None
        self._user_clip = None    # the user's clipboard, saved before the agent's first clipboard('set')
        self._our_clip = None     # what the agent put there
        self.recording: list | None = None
        self.tokens = 0
        self.task_tokens = 0
        self.actions = 0
        self.overlay = OverlayClient(self._on_overlay)
        self.input = Input(on_move=lambda x, y: self.overlay.send({"op": "move", "x": x, "y": y}),
                           on_event=self._input_event)
        self._apps: list[dict] | None = None
        self._idle_timer: threading.Timer | None = None
        try:
            self.idle_hide = float(self.gate.policy.rules.get("hide_overlay_after_idle_seconds", 30) or 0)
        except (TypeError, ValueError):
            self.idle_hide = 30.0
        try:  # when the user grabs the mouse: wait this long for them to let go (0 = give up at once)
            self.user_wait = float(self.gate.policy.rules.get("wait_for_user_mouse_seconds", 60))
            self.user_idle = float(self.gate.policy.rules.get("user_mouse_idle_seconds", 1.5) or 1.5)
        except (TypeError, ValueError):
            self.user_wait, self.user_idle = 60.0, 1.5
        self.look_first = bool(self.gate.policy.rules.get("look_before_acting", True))
        self.screen_lock = ScreenLock()
        # Admin mode: "ask" (default) = the AI may ask to turn it on (a DeskPilot dialog, then Windows' UAC prompt)
        # and it turns off again at done(); "always" = turn it on at the first call and keep it for the session;
        # "off" = never. See elevation.py.
        self.admin = elevation.AdminHelper()
        self.admin_mode = str(self.gate.policy.rules.get("admin_mode", "ask")).strip().lower()
        if self.admin_mode not in ("ask", "always", "off"):
            self.admin_mode = "ask"
        self._admin_declined = False
        try:
            self.admin_idle = float(self.gate.policy.rules.get("admin_idle_minutes", 5) or 0) * 60
        except (TypeError, ValueError):
            self.admin_idle = 300.0
        self._admin_timer: threading.Timer | None = None
        try:
            keep = float(self.gate.policy.rules.get("timeline_keep_days", 7))
        except (TypeError, ValueError):
            keep = 7.0
        self.timeline = TL.Timeline(bool(self.gate.policy.rules.get("timeline", True)), keep, self.screen_lock.label)
        self._typed_secret = False
        try:
            self.budget = int(self.gate.policy.rules.get("token_budget_per_task", 100000) or 0)
        except (TypeError, ValueError):
            self.budget = 100000

    # ------------------------------------------------------------ overlay lifetime
    def busy(self):
        """A tool call started: keep the overlay up."""
        if self._idle_timer:
            self._idle_timer.cancel()
            self._idle_timer = None
        if self._admin_timer:
            self._admin_timer.cancel()
            self._admin_timer = None

    def idle(self):
        """A tool call ended: hide the overlay if the agent stays quiet (it may have finished without done())."""
        self.busy()
        if self.idle_hide > 0:
            self._idle_timer = threading.Timer(self.idle_hide, self.finish, kwargs={"reason": "idle"})
            self._idle_timer.daemon = True
            self._idle_timer.start()
        if self.admin.active and self.admin_mode != "always" and self.admin_idle > 0:
            # The overlay hides after a short silence, but admin mode waits longer: the agent may just be thinking,
            # and turning it off would mean another UAC prompt for the user.
            self._admin_timer = threading.Timer(self.admin_idle, self._admin_expired)
            self._admin_timer.daemon = True
            self._admin_timer.start()

    def _admin_expired(self):
        if self.admin.active:
            self.call_admin_off()
            audit({"event": "admin", "on": False, "reason": "idle"})

    def finish(self, summary: str = "", reason: str = "done") -> str:
        """Task over: drop the overlay. A user Pause/Stop stays in effect (the next call shows it again);
        an automatic 'you moved the mouse' pause is cleared so the next task starts clean."""
        if self.pause_reason in ("moved", "budget"):
            self.pause_reason = ""
            self.overlay.send({"op": "paused", "v": ""})
        self.task_tokens = 0
        steps, self.task_log = self.task_log, []
        self._moving = None
        restored = self._restore_user_clip() if self._user_clip is not None else ""
        admin_note = ""
        if self.admin.active and self.admin_mode != "always" and reason == "done":
            self.call_admin_off()
            admin_note = "Admin mode is off again (it lasts one task)."
        if self.screen_lock.held:
            self.screen_lock.release()
            self.timeline.record("✓ " + (summary or "task finished") if reason == "done" else "task went idle",
                                 kind="mark")
        self._send_stats()
        if summary:
            self.ui_log("✓ " + summary)
        self.ui_state("idle")
        self.overlay.hide()
        audit({"event": "finish", "reason": reason, **({"summary": summary[:200]} if summary else {})})
        out = "Overlay hidden. Tell the user what you did." + (
            f" Note: the user has you {self.pause_reason.upper()}; they unlock it from the panel next time."
            if self.paused else "")
        if restored:
            out += f" ({restored})"
        if admin_note:
            out += " " + admin_note
        if reason == "done" and steps:
            shown = steps[-40:]
            out += f"\nWhat you did this task ({len(steps)} steps" + (", last 40" if len(steps) > 40 else "") + "):\n"
            out += "\n".join(shown)
            if self.timeline.enabled:
                try:
                    out += f"\nStep-by-step pictures for the user: {TL.build_html()}"
                except OSError:
                    pass
        return out

    # ------------------------------------------------------------ plumbing
    def _init_thread(self):
        self.uia = elevation.RoutedUIA(UIA(), self.admin)

    def call(self, fn, *a, **kw):
        if fn == self.finish:
            return self.pool.submit(fn, *a, **kw)

        def guarded():
            self._check_killed()  # every tool, not only the ones that go through ensure_ready
            return fn(*a, **kw)
        return self.pool.submit(guarded)

    @property
    def paused(self) -> bool:
        return bool(self.pause_reason)

    def _set_pause(self, reason: str):
        self.pause_reason = reason
        self.overlay.send({"op": "paused", "v": reason})
        audit({"event": "pause", "reason": reason or "resumed"})

    def _on_overlay(self, cmd: str):
        if cmd == "PANIC":
            self._set_pause("" if self.paused else "paused")
        elif cmd in ("PAUSE", "TAKEOVER", "STOP"):
            self._set_pause({"PAUSE": "paused", "TAKEOVER": "takeover", "STOP": "stopped"}[cmd])
        elif cmd in ("RESUME", "GIVEBACK"):
            if self.pause_reason in ("paused", "takeover", "moved", "stopped"):
                self.last_look = 0.0  # the user may have changed the screen meanwhile
            if self.pause_reason == "budget":
                self.task_tokens = 0  # the user allowed another budget's worth
                self._send_stats()
            self._set_pause("")
        elif cmd.startswith("MODE "):
            self.session.mode = cmd.split()[1]
            self.session.save()
            audit({"event": "mode", "mode": self.session.mode})

    def _confirm_with_ui(self, title: str, detail: str) -> str:
        self.ui_state("asking", title)
        try:
            return self._dialog(title, detail)
        finally:
            # The user just moved the mouse to click Allow/Deny: that is not "taking over".
            self.input.grace(1.5)
            self.ui_state("working")

    def _yield_to_user(self) -> float:
        """The human grabbed the mouse: step aside, show that on screen, and continue once they let go."""
        self.ui_state("waiting")
        self.ui_log("✋ you have the mouse · continuing when you let go")
        audit({"event": "yield"})
        try:
            return self.input.wait_for_user(idle=self.user_idle, timeout=self.user_wait, check=self._check_paused)
        finally:
            self.ui_state("working")

    def _input_event(self, kind, **kw):
        if kind == "click":
            self.overlay.send({"op": "click", "x": kw["x"], "y": kw["y"]})
        elif kind == "label":
            self.overlay.send({"op": "label", "text": kw["text"]})

    def ui_state(self, state: str, text: str = ""):
        self.overlay.send({"op": "state", "v": state, "text": text})

    def ui_log(self, text: str):
        self.overlay.send({"op": "log", "text": text})

    def add_tokens(self, n: int):
        self.tokens += n
        self.task_tokens += n
        self._send_stats()
        if self.budget and self.task_tokens >= self.budget and not self.paused:
            self._set_pause("budget")
            self.ui_log(f"⏸ token budget reached (~{self.task_tokens // 1000}k)")

    def _send_stats(self):
        self.overlay.send({"op": "stats", "tokens": self.tokens, "actions": self.actions,
                           "task": self.task_tokens, "budget": self.budget})

    def _check_paused(self):
        if self.paused:
            raise AgentError(PAUSE_MSG[self.pause_reason])

    def _check_killed(self):
        """After the kill switch, nothing happens on the desktop until the user says so in person."""
        if not killswitch.killed():
            return
        answer = self._dialog("control your computer again",
                              "You used the DeskPilot kill switch. The AI is asking to use your screen again.\n"
                              "Deny keeps DeskPilot switched off.")
        if answer == "deny":
            raise AgentError("KILLED: the user hit the DeskPilot kill switch and has not allowed control again. "
                             "Do not use DeskPilot tools; tell the user and wait for their instructions.")
        killswitch.clear_flag()
        audit({"event": "unkill", "answer": answer})

    def ensure_ready(self):
        if not self.session.picked:
            self.pick()
        self.overlay.start(self.session.areas(), self.session.mode)
        self._check_paused()
        if self.admin_mode == "always" and not self.admin.active and not self._admin_declined:
            try:
                self.admin_on()
            except AgentError:
                pass  # declined or failed: admin windows report themselves as blocked; don't ask again this run

    # ------------------------------------------------------------ admin mode (elevation.py)
    def _wire_admin(self, on: bool) -> None:
        act_mod.remote_sender = (lambda items: self.admin.call("input", items=items)) if on else None
        winutil.remote = self.admin.call if on else None

    def admin_on(self) -> str:
        """Start the admin helper (UAC prompt) so input and reads reach windows that run as administrator."""
        if self.admin.active:
            return "Admin mode is already on: you can click and type in windows that run as administrator."
        if elevation.own_level() >= elevation.HIGH:
            return "DeskPilot already runs as administrator, so it can already use admin windows. Nothing to do."
        if self.admin_mode == "off":
            raise AgentError("Admin mode is switched off in the user's DeskPilot policy (admin_mode: off). Ask the "
                             "user to do the step themselves, or to change admin_mode in the policy file.")
        if self.admin_mode == "ask":
            self._require({"kind": "admin"},
                          detail="The AI wants to click and type in windows that run as administrator (installers, "
                                 "Task Manager, hardware tools, some settings). Windows blocks that unless you allow "
                                 "DeskPilot's admin helper.\n\nIf you allow it here, Windows shows a UAC prompt next "
                                 "(it names Python, which runs DeskPilot). Admin mode ends when this task is done.")
        self.ui_state("asking", "approve the Windows prompt to turn on admin mode")
        self.ui_log("\U0001F6E1 asking Windows for admin mode")
        try:
            self.admin.start()
        except elevation.HelperError as e:
            self._admin_declined = True
            audit({"event": "admin", "on": False, "error": str(e)})
            raise AgentError(f"Admin mode is NOT on: {e}. Ask the user to do the admin step themselves.")
        finally:
            self.input.grace(1.5)  # the user just used the mouse on the prompt
            self.ui_state("working")
        self._admin_declined = False
        self._wire_admin(True)
        self.ui_log("\U0001F6E1 admin mode on")
        audit({"event": "admin", "on": True, "mode": self.admin_mode})
        self.last_look = 0.0  # the prompt took time; look again before acting
        return ("ADMIN MODE ON: your clicks, typing and window commands now also reach windows that run as "
                "administrator, and snapshot() can read them. Windows' own UAC prompts still can't be clicked by "
                "anyone but the user. " + ("It stays on for this session." if self.admin_mode == "always" else
                                           "It turns off when you call done(), or after a few quiet minutes.")
                + " Take a snapshot, then act.")

    def call_admin_off(self) -> None:
        self._wire_admin(False)
        if self.admin.active:
            self.admin.stop()
            self.ui_log("\U0001F6E1 admin mode off")
            audit({"event": "admin", "on": False})

    def admin_off(self) -> str:
        was = self.admin.active
        self.call_admin_off()
        return "Admin mode is off." if was else "Admin mode was not on."

    def _admin_blocked(self, hwnd: int) -> None:
        """Refuse an input step aimed at an admin window Windows won't let us reach (instead of a click that
        silently lands on the window behind it)."""
        if self.admin.active or not hwnd or not elevation.window_above_us(hwnd):
            return
        root = winutil.user32.GetAncestor(hwnd, 2) or hwnd
        title = win32gui.GetWindowText(root)
        exe = winutil._exe_name(elevation.window_pid(root))
        audit({"event": "admin_blocked", "title": title[:80], "exe": exe})
        raise AgentError(elevation.blocked_message(title or "this window", exe, self.admin_mode != "off"))

    def _guard_keys(self) -> None:
        """Keys go to the foreground window: refuse if that is an admin window we can't reach."""
        self._admin_blocked(winutil.foreground())

    def pick(self) -> str:
        try:
            out = subprocess.run([sys.executable, "-m", "deskpilot.picker"], capture_output=True, text=True,
                                 timeout=200, creationflags=NO_WINDOW).stdout.strip()
            res = json.loads(out or "null")
        except Exception as e:
            raise AgentError(f"screen picker failed: {e}")
        if not res:
            raise AgentError("The user closed the screen picker without choosing a screen. Ask them to choose one.")
        self.session.monitor, self.session.mode, self.session.remember = res["monitor"], res["mode"], res["remember"]
        self.session.picked = True
        self.session.save()
        self.shot = None
        self.overlay.start(self.session.areas(), self.session.mode)
        audit({"event": "picked", **res})
        return self.info()

    @property
    def areas(self) -> list[Rect]:
        return self.session.areas()

    @property
    def strict(self) -> bool:
        return self.session.mode == "strict"

    def wref(self, hwnd: int) -> str:
        if hwnd not in self.win_refs:
            ref = f"w{len(self.win_refs) + 1}"
            self.win_refs[hwnd] = ref
            self.win_by_ref[ref] = hwnd
        return self.win_refs[hwnd]

    def _screen_label(self) -> str:
        if self.session.monitor == "all":
            return "all screens"
        a = self.session.primary_area()
        return f"screen {int(self.session.monitor) + 1} ({a.width}x{a.height})"

    def _private(self, w: winutil.Win) -> bool:
        return self.privacy.is_private(w.title, w.exe())

    # ------------------------------------------------------------ info
    def info(self) -> str:
        mons = monitors()
        lines = [f"controlling: {self._screen_label()}  mode: {self.session.mode}"
                 + (f"  {self.pause_reason.upper()}" if self.paused else "")]
        for i, m in enumerate(mons):
            sel = self.session.monitor == "all" or self.session.monitor == i
            lines.append(f" screen {i + 1}: {m.width}x{m.height} at {m.left},{m.top}{' *' if sel else ''}")
        top = winutil.top_window(self.areas)
        if top:
            title = "(private)" if self._private(top) else f'"{top.title[:70]}"'
            admin = " (runs as administrator)" if elevation.window_above_us(top.hwnd) else ""
            lines.append(f"top window: {self.wref(top.hwnd)} {title}{admin}")
        if self.admin.active:
            lines.append("admin mode: ON (input reaches admin windows)")
        elif elevation.own_level() >= elevation.HIGH:
            lines.append("admin mode: DeskPilot itself runs as administrator")
        else:
            lines.append("admin mode: off" + (" (screen_info(op='admin') asks the user)" if self.admin_mode != "off"
                                              else " (disabled in policy)"))
        return "\n".join(lines)

    # ------------------------------------------------------------ browser pages
    def _page(self, w: winutil.Win):
        if w.exe().lower() not in BROWSER_EXES:
            return None
        try:
            return self.browsers.page_for(w.title, w.pid, w.exe())
        except Exception:
            return None

    @staticmethod
    def _content_rect(hwnd: int):
        best = []

        def cb(h, _):
            if win32gui.GetClassName(h) == "Chrome_RenderWidgetHostHWND" and win32gui.IsWindowVisible(h):
                l, t, r, b = win32gui.GetWindowRect(h)
                best.append(((r - l) * (b - t), (l, t, r, b)))
            return True

        try:
            win32gui.EnumChildWindows(hwnd, cb, None)
        except Exception:
            pass
        return max(best)[1] if best else None

    def _web_items(self, w, page, full: bool, filt: str | None, limit: int):
        res = self.browsers.extract(page, full, filt, limit)
        cr = self._content_rect(w.hwnd) or w.rect
        scale = (cr[2] - cr[0]) / max(1, res["vw"])
        items = []
        for it in res["items"]:
            role = WEB_ROLE.get(it["role"], it["role"])
            rect = (int(cr[0] + it["x"] * scale), int(cr[1] + it["y"] * scale),
                    int(cr[0] + (it["x"] + it["w"]) * scale), int(cr[1] + (it["y"] + it["h"]) * scale))
            self.web[it["id"]] = {"page": page, "hwnd": w.hwnd, "name": it["name"], "role": role, "rect": rect,
                                  "editable": role in ("edit", "combo"), "password": it["value"] == "***"}
            parts = [] if role == "text" else [role]
            if it["name"]:
                parts.append(f'"{it["name"]}"')
            if it["value"] not in (None, "") and it["value"] != it["name"]:
                parts.append(f'="{it["value"]}"')
            if it["flags"]:
                parts.append(it["flags"])
            items.append((it["id"], " ".join(parts)))
        return res, items

    def _web_block(self, w, page, full: bool, filt: str | None, limit: int) -> tuple[list[str], dict]:
        try:
            res, items = self._web_items(w, page, full, filt, limit)
        except Exception as e:
            return [f"(page not readable: {str(e)[:80]})"], {}
        lines = [f'page {_clip(res["url"], 100)}  scroll {res["scrollY"]}/{res["scrollH"]}']
        by_ref = {}
        for ref, desc in items:
            lines.append(f"{ref} {desc}")
            by_ref[ref] = f"{ref} {desc}"
        if res["more"]:
            lines.append(f"…{res['more']} more (use find or filter)")
        if not full and not filt and res["off"]:
            lines.append(f"({res['off']} more off-screen: scroll, find(), or snapshot(scope='page'))")
        return lines, by_ref

    # ------------------------------------------------------------ perception
    def _window_block(self, w: winutil.Win, max_nodes: int, filt: str | None, full_page=False) -> tuple[list, dict]:
        if self._private(w):
            return ["(private window: hidden by privacy settings - the user must handle it; ask them)"], {}
        page = self._page(w)
        if page:
            return self._web_block(w, page, full_page, filt, max_nodes)
        note = []
        if not self.admin.active and elevation.window_above_us(w.hwnd):
            note = ["(ADMIN WINDOW: this app runs as administrator. Windows hides its elements from DeskPilot and "
                    "drops clicks and typing into it. Screenshots and snapshot(scope='ocr') still read it. To act in "
                    "it, ask the user to do the step"
                    + (", or call screen_info(op=\"admin\") to ask them to turn on admin mode (UAC prompt)"
                       if self.admin_mode != "off" else "") + ".)"]
        try:
            tree = self.uia.tree(w.hwnd)
        except Exception as e:
            return [f"(cannot read window: {e.__class__.__name__}; try scope='ocr')"], {}
        lines, by_ref, hidden = format_tree(tree, self.areas, self.refs, max_nodes, filt)
        if hidden:
            lines.append(f"…{hidden} more elements hidden (use find, or snapshot with filter)")
        if not lines and not note:
            lines.append("(no accessible elements; use scope='ocr' or screenshot)")
        return note + lines, by_ref

    def snapshot(self, scope: str = "window", filter: str | None = None, max_nodes: int = 250,
                 region=None) -> str:
        self.ensure_ready()
        if scope == "ocr" or region is not None:
            self.last_look = time.time()
            return self._ocr_snapshot(filter, None if region is None else self._region_rect(region))
        if elevation.secure_desktop():
            self.last_look = time.time()
            return elevation.SECURE_DESKTOP_MSG
        wins = [w for w in winutil.windows() if winutil.on_area(w, self.areas)]
        out = [f"{self._screen_label()}"]
        by_ref_all: dict[str, str] = {}
        if scope == "taskbar":
            for h in self._taskbars():
                lines, by_ref, _ = format_tree(self.uia.tree(h), self.areas, self.refs, max_nodes, filter)
                out += lines
                by_ref_all.update(by_ref)
            return "\n".join(out) if len(out) > 1 else "taskbar not found on the controlled screen"
        if not wins:
            return out[0] + "\nno windows on this screen. Use launch(...) to open an app."
        targets = wins if scope == "screen" else wins[:1]
        per = max(40, max_nodes // len(targets))
        for w in targets:
            out.append(f"window {self.wref(w.hwnd)} \"{w.title[:80]}\" ({w.exe()})")
            lines, by_ref = self._window_block(w, per, filter, full_page=scope == "page")
            out += lines
            by_ref_all.update(by_ref)
        if scope not in ("screen",) and len(wins) > 1:
            out.append("other windows: " + ", ".join(f'{self.wref(w.hwnd)} "{w.title[:40]}"' for w in wins[1:8]))
        self.last = {"hwnd": wins[0].hwnd, "by_ref": by_ref_all, "scope": scope, "filter": filter}
        self.last_look = time.time()
        return "\n".join(out)

    def _taskbars(self) -> list[int]:
        res = []
        for cls in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            h = win32gui.FindWindow(cls, None)
            while h:
                l, t, r, b = win32gui.GetWindowRect(h)
                if any(a.intersects(l, t, r, b) for a in self.areas):
                    res.append(h)
                h = win32gui.FindWindowEx(None, h, cls, None)
        return res

    def _private_rects(self, area: Rect) -> list[tuple]:
        rects = []
        for w in winutil.windows():
            l, t, r, b = w.rect
            if area.intersects(l, t, r, b) and self._private(w):
                rects.append(w.rect)
        for info in list(self.refs.info.values()) + [type("I", (), v) for v in self.web.values()]:
            if getattr(info, "password", False):
                rects.append(info.rect)
        return rects

    def _grab_safe(self, area: Rect):
        img = grab(area)
        return black_out(img, area, self._private_rects(area))

    def _ocr_snapshot(self, filt: str | None, region: Rect | None = None) -> str:
        if region:
            out = [f"text in {region.left},{region.top} {region.width}x{region.height} (OCR; refs are clickable)"]
        else:
            out = [f"{self._screen_label()} (OCR text; refs are clickable)"]
        for a in [region] if region else self.areas:
            for ln in ocr(a, self._grab_safe(a)):
                if filt and filt.lower() not in ln.text.lower():
                    continue
                ref = self.refs.assign("ocr", ln.text, "", ln.rect)
                out.append(f'{ref} "{ln.text[:100]}"')
        return "\n".join(out) if len(out) > 1 else out[0] + "\n(no text found)"

    def find(self, text: str | None = None, role: str | None = None, limit: int = 15) -> str:
        self.ensure_ready()
        hits = self._find(text, role, limit)
        self.last_look = time.time()
        if not hits:
            return f"not found: {text or role}"
        return "\n".join(hits)

    def _find(self, text, role, limit) -> list[str]:
        out: list[str] = []
        for w in [w for w in winutil.windows() if winutil.on_area(w, self.areas)][:6]:
            if self._private(w):
                continue
            page = self._page(w)
            if page:
                try:
                    _, items = self._web_items(w, page, True, text, 200)
                except Exception:
                    items = []
                if role:
                    items = [i for i in items if self.web[i[0]]["role"] == role]
                # Prefer interactive, then exact name match.
                t = (text or "").lower()
                items.sort(key=lambda i: (self.web[i[0]]["role"] == "text",
                                          self.web[i[0]]["name"].lower() != t, len(self.web[i[0]]["name"])))
                out += [f"{ref} {desc}" for ref, desc in items[:limit - len(out)]]
            else:
                try:
                    nodes = find_nodes(self.uia.tree(w.hwnd), text, role, self.areas)
                except Exception:
                    continue
                for n in nodes[:limit - len(out)]:
                    ref = self.refs.assign(n.role, n.name, n.aid, n.rect, n.role in EDITABLE, n.password)
                    out.append(f"{ref} {describe(n)}")
            if len(out) >= limit:
                break
        seen, uniq = set(), []
        for line in out:
            if line.split()[0] not in seen:
                seen.add(line.split()[0])
                uniq.append(line)
        out = uniq
        if not out and text and role in (None, "text", "ocr"):
            for a in self.areas:
                for match, rect in ocr_match(ocr(a, self._grab_safe(a)), text)[:limit]:
                    ref = self.refs.assign("ocr", match, "", rect)
                    out.append(f'{ref} ocr "{match}"')
        return out

    def screenshot(self, ref: str | None = None, region: list[int] | None = None, marks: bool = False,
                   max_side: int = 1280, screen: int | None = None):
        self.ensure_ready()
        area = self.areas[0]
        if screen is not None and self.session.monitor == "all":
            area = self.areas[max(0, min(screen - 1, len(self.areas) - 1))]
        if ref:
            if ref == "window" or re.fullmatch(r"w\d+", ref):
                rect = self._window_target(None if ref == "window" else ref).rect
                pad = 0
            else:
                rect = self._ref_rect(ref)
                pad = 40
            l, t, r, b = rect
            nl, nt = max(area.left, l - pad), max(area.top, t - pad)
            area = Rect(nl, nt, max(10, min(r + pad, area.right) - nl), max(10, min(b + pad, area.bottom) - nt))
        elif region:
            x, y, w, h = region
            ox, oy, sc = self.shot or (area.left, area.top, 1.0)
            area = Rect(int(ox + x / sc), int(oy + y / sc), int(w / sc), int(h / sc))
        img, ms, settled = self._settle(area)
        black = self._black_hint(img, area)
        if marks:
            infos = [self.refs.get(r) for r in self.last.get("by_ref", {}) if r.startswith("e")]
            infos += [type("I", (), {"ref": r, **self.web[r]}) for r in self.last.get("by_ref", {}) if r in self.web]
            draw_marks(img, area, [i for i in infos if i])
        data, scale = encode_jpeg(img, max_side)
        self.shot = (area.left, area.top, scale)
        self.last_look = time.time()
        w, h = int(img.width * scale), int(img.height * scale)
        when = (f"settled: nothing moved for a moment ({ms} ms after the call)" if settled else
                f"the screen was STILL CHANGING after {ms} ms (animation or loading): look again before relying on it")
        if self._moving and settled:
            when += " (ignoring one area that keeps moving, e.g. a video)"
        crop = bool(ref or region)
        note = (f"{w}x{h} image of {'a CROP of ' if crop else ''}the screen; {when}. "
                f"Click what you see with [x,y] in THESE image pixels, e.g. act([{{\"click\": [x, y]}}])"
                + (" (DeskPilot maps them back to the screen; [x,y] from earlier screenshots no longer apply)"
                   if crop else "; region=[x,y,w,h] zooms in") + ".")
        return data, (black + "\n" + note) if black else note

    def _settle(self, area: Rect, max_wait: float = 1.2):
        """Grab the screen once it has stopped changing (two frames ~80 ms apart that match), so the picture
        shows the result of the last action rather than the frame before it. Returns (masked image, ms, settled)."""
        start = time.time()
        img = grab(area)
        prev = cur = thumb(img)
        settled = False
        last_box = None
        same = self._last_thumb is not None and self._last_thumb.size == prev.size
        mask = self._moving if same else None  # a video found by the previous call is probably still playing
        while time.time() - start < max_wait:
            time.sleep(0.08)
            img = grab(area)
            cur = thumb(img)
            box = diff_box(prev, cur, mask)
            if box is None:
                settled = True
                break
            # The same spot changing frame after frame is a video or animation, not the app reacting: ignore it.
            m = moving_box(last_box, box, cur.size)
            if m:
                mask = m if mask is None else (min(mask[0], m[0]), min(mask[1], m[1]),
                                               max(mask[2], m[2]), max(mask[3], m[3]))
            last_box, prev = box, cur
        self._moving, self._last_thumb = mask, cur
        return black_out(img, area, self._private_rects(area)), int((time.time() - start) * 1000), settled

    def _black_hint(self, img, area: Rect) -> str:
        """An all-black capture usually means the wrong screen (or a locked/sleeping one): say where windows are.
        While a UAC prompt is up, say that instead (the picture is black or frozen then)."""
        if elevation.secure_desktop():
            return elevation.SECURE_DESKTOP_MSG
        if not is_black(img):
            return ""
        mons = monitors()
        elsewhere = []
        for w in winutil.windows()[:12]:
            if winutil.on_area(w, self.areas) or self._private(w):
                continue
            cx, cy = w.center
            scr = next((i + 1 for i, m in enumerate(mons) if m.contains(cx, cy)), None)
            if scr:
                elsewhere.append(f'{self.wref(w.hwnd)} "{w.title[:40]}" is on screen {scr}')
        msg = (f"WARNING: this image is (almost) all black. You control {self._screen_label()}; it may be off, "
               "asleep, locked, or showing protected video.")
        if elsewhere:
            msg += (" Windows on other screens: " + "; ".join(elsewhere[:5]) + ". Bring one here with "
                    "window('move_here', 'wN'), or ask the user to let you control that screen "
                    "(screen_info(op='switch', screen='N')).")
        return msg

    # ------------------------------------------------------------ targets
    def _ref_rect(self, ref: str):
        if ref in self.web:
            return self.web[ref]["rect"]
        info = self.refs.get(ref)
        if not info:
            raise AgentError(f"unknown ref {ref}; take a new snapshot")
        return info.rect

    def _locate_web(self, ref: str) -> tuple[int, int]:
        """Screen point to click a b-ref NOW. Asks the page where the element is (scrolling it into view inside its
        list/dropdown if needed), waits until it stops moving (lists that slide open, pages that scroll), and refuses
        to click when something else covers it, so a click never lands on a neighbour."""
        info = self.web[ref]
        prev = None
        r = None
        for _ in range(8):
            try:
                r = self.browsers.locate(info["page"], ref)
            except Exception as e:
                raise AgentError(f"page changed ({str(e)[:60]}); take a new snapshot")
            if not r:
                raise AgentError(f"{ref} is gone from the page; take a new snapshot")
            pos = (round(r["cx"]), round(r["cy"]), r.get("sy"))
            if pos == prev:
                break
            prev = pos
            time.sleep(0.08)
        if r.get("covered"):
            raise AgentError(f'{ref} "{info["name"][:40]}" is hidden behind "{r["covered"]}" (a popup, menu or '
                             "header); close or scroll it first, or take a screenshot to see the screen")
        cr = self._content_rect(info["hwnd"])
        if not cr:
            x, y = ((info["rect"][0] + info["rect"][2]) // 2, (info["rect"][1] + info["rect"][3]) // 2)
            return x, y
        scale = (cr[2] - cr[0]) / max(1, r["vw"])
        info["rect"] = (int(cr[0] + r["x"] * scale), int(cr[1] + r["y"] * scale),
                        int(cr[0] + (r["x"] + r["w"]) * scale), int(cr[1] + (r["y"] + r["h"]) * scale))
        return int(cr[0] + r["cx"] * scale), int(cr[1] + r["cy"] * scale)

    def _locate_uia(self, ref: str, info) -> tuple[int, int]:
        """Screen point to click an e-ref NOW. The snapshot position goes stale when the page scrolls or a list
        expands; check what is under that point and, if it is no longer the element, find where it moved to."""
        x, y = info.center
        if info.role == "ocr" or not info.name:
            return x, y
        rect = None
        last = None
        for _ in range(4):  # wait out slide-open animations / smooth scrolling
            rect = self.uia.relocate(x, y, info.role, info.name, info.rect)
            if rect is None or rect == last:
                break
            last = rect
            time.sleep(0.1)
        if rect is None:
            return x, y
        info.rect = tuple(rect)
        return info.center

    def _resolve(self, target) -> tuple[int, int, str, dict]:
        """target -> (x, y, name, info). Accepts ref 'e12'/'b12', [x, y] image/screen px, visible text,
        or {"ocr": "Save Widget", "nth": 1, "in": region} (text found on the screen image by OCR)."""
        if isinstance(target, (list, tuple)) and len(target) == 2:
            ox, oy, sc = self.shot or (self.areas[0].left, self.areas[0].top, 1.0)
            x, y = int(ox + target[0] / sc), int(oy + target[1] / sc)
            if not self.session.allowed(x, y):
                crop = self.shot and (ox, oy) != (self.areas[0].left, self.areas[0].top)
                raise AgentError(
                    f"[{target[0]}, {target[1]}] maps to screen point ({x},{y}), outside the screen you control. "
                    "[x,y] are read in the pixels of your LAST screenshot"
                    + (f", which was a crop starting at screen ({ox},{oy}) at scale {sc:.2f}" if crop else "")
                    + ". Use coordinates from that image, or take a new screenshot() first")
            n = self.uia.at_point(x, y)
            return x, y, (n.name if n else ""), {"role": n.role if n else "", "rect": n.rect if n else None}
        if isinstance(target, dict) and "ocr" in target:
            name, rect = self._ocr_locate(str(target["ocr"]), int(target.get("nth", 1) or 1), target.get("in"))
            return (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2, name, {"role": "ocr", "rect": rect}
        if isinstance(target, str):
            t = target.strip()
            if re.fullmatch(r"b\d+", t):
                if t not in self.web:
                    raise AgentError(f"unknown ref {t}; take a new snapshot")
                x, y = self._locate_web(t)
                i = self.web[t]
                return x, y, i["name"], {"role": i["role"], "editable": i["editable"], "password": i["password"],
                                         "rect": i["rect"]}
            if re.fullmatch(r"e\d+", t):
                info = self.refs.get(t)
                if not info:
                    raise AgentError(f"unknown ref {t}; take a new snapshot")
                x, y = self._locate_uia(t, info)
                return x, y, info.name, {"role": info.role, "editable": info.editable, "password": info.password,
                                         "rect": info.rect}
            hits = self._find(t, None, 1)
            if hits:
                return self._resolve(hits[0].split()[0])
            raise AgentError(f'no element with text "{t}" on screen')
        raise AgentError(f"bad target {target!r}")

    def _ocr_locate(self, text: str, nth: int = 1, where=None) -> tuple[str, tuple[int, int, int, int]]:
        """Find text on the screen image by OCR (canvas apps such as Flutter web have no accessible elements).
        Exact word matches come first, then reading order (top to bottom, left to right)."""
        areas = [self._region_rect(where)] if where is not None else self.areas
        hits = []
        for a in areas:
            hits += ocr_match(ocr(a, self._grab_safe(a)), text)
        if not hits:
            raise AgentError(f'"{text}" not found on the screen by OCR; take a screenshot to see what is there')
        q = text.lower().strip()
        hits.sort(key=lambda h: (h[0].lower().strip() != q, h[1][1] // 12, h[1][0]))
        if nth > len(hits):
            raise AgentError(f'only {len(hits)} match(es) for "{text}" on screen, not {nth}')
        return hits[nth - 1]

    def _region_rect(self, spec) -> Rect:
        """A screen area: None/"screen" = your screen, "window"/"w3" = a window, "e12"/"b12" = an element,
        [x,y,w,h] = pixels of the last screenshot (like screenshot(region=...)), {"ocr": "text"} = around that text."""
        area = self.areas[0]
        if spec in (None, True, "screen", ""):
            return area
        if isinstance(spec, dict) and "ocr" in spec:
            _, (l, t, r, b) = self._ocr_locate(str(spec["ocr"]), int(spec.get("nth", 1) or 1))
            pad = int(spec.get("pad", 12))
            l, t, r, b = l - pad, t - pad, r + pad, b + pad
        elif isinstance(spec, (list, tuple)) and len(spec) == 4:
            x, y, w, h = (float(v) for v in spec)
            ox, oy, sc = self.shot or (area.left, area.top, 1.0)
            l, t = int(ox + x / sc), int(oy + y / sc)
            r, b = l + int(w / sc), t + int(h / sc)
        elif isinstance(spec, str) and (spec == "window" or re.fullmatch(r"w\d+", spec)):
            l, t, r, b = self._window_target(None if spec == "window" else spec).rect
        elif isinstance(spec, str) and re.fullmatch(r"[eb]\d+", spec):
            l, t, r, b = self._ref_rect(spec)
        else:
            raise AgentError(f"bad region {spec!r}: use [x,y,w,h], 'window', a ref, or {{\"ocr\": \"text\"}}")
        a = next((a for a in self.areas if a.intersects(l, t, r, b)), None)
        if not a:
            raise AgentError(f"region {spec!r} is outside the screen you control")
        l, t = max(l, a.left), max(t, a.top)
        r, b = min(r, a.right), min(b, a.bottom)
        return Rect(l, t, max(4, r - l), max(4, b - t))

    def _name_of(self, ref: str) -> str:
        if ref in self.web:
            return self.web[ref]["name"]
        info = self.refs.get(ref)
        return info.name if info else ""

    def _guard_point(self, x, y, admin_check: bool = True):
        if not self.session.allowed(x, y):
            raise AgentError(f"({x},{y}) is outside the screen you are allowed to control")
        h = win32gui.WindowFromPoint((x, y))
        root = winutil.user32.GetAncestor(h, 2) if h else 0
        if root:
            title = win32gui.GetWindowText(root)
            try:
                import win32process

                exe = winutil._exe_name(win32process.GetWindowThreadProcessId(root)[1])
            except Exception:
                exe = ""
            if self.privacy.is_private(title, exe):
                raise AgentError("that point is inside a private window (privacy settings); ask the user")
            if admin_check:
                self._admin_blocked(root)

    # ------------------------------------------------------------ actions
    def _orient(self) -> str | None:
        """First action of a task without having looked: show the screen instead of acting on assumptions
        (another window may be on top, the page may differ, old refs may point at the wrong thing)."""
        if not self.look_first or (self.last_look and time.time() - self.last_look < self.look_fresh):
            return None
        self.ui_log("👀 looking at the screen before acting")
        snap = self.snapshot()
        audit({"event": "orient"})
        return (f"NOT DONE YET: you haven't looked at the screen in the last {self.look_fresh:g}s (or the user paused "
                "or took over since), so here is the screen as it is right now. "
                "Read it: which window is on top, what page or state it is in, what is focused. Then send act "
                "again with steps that fit THIS screen (use the refs below; refs from before may be stale). "
                "If what you need is not here, use launch() or window().\n" + snap)

    def _claim(self):
        """Take the PC-wide one-AI-at-a-time lock before touching mouse or keyboard."""
        try:
            self.screen_lock.claim()
        except Busy as e:
            self.ui_log("⏳ another AI session has the computer")
            raise AgentError(str(e))

    def _log_step(self, st, ok: bool = True, why: str = "", img=None):
        """Timeline entry for one step, with a small picture of the screen right after it (already masked)."""
        text = self._safe_step_text(st)
        self.task_log.append(f"{len(self.task_log) + 1}. {text}" + ("" if ok else f"  ✗ {why[:80]}"))
        if not self.timeline.enabled:
            return
        if not ok:
            text += f"  ✗ {why[:120]}"
        self.timeline.record(text, ok=ok, img=img if ok else None)

    def _safe_step_text(self, st) -> str:
        if self._typed_secret or (isinstance(st, dict) and "type" in st and st.get("secret")):
            return "type *** (password field)"
        return self.privacy.text(_step_text(st))

    def _after_step(self, st, before):
        """Screen right after a step, and whether it changed. Steps that should change something get up to
        0.6 s for the app to redraw, so a slow repaint isn't reported as 'nothing happened'."""
        area = self.areas[0]
        deadline = time.time() + (0.6 if _kind(st) in EFFECT_STEPS else 0)
        try:
            while True:
                img = grab(area)
                th = thumb(img)
                moved = changed(before, th, self._moving)
                if moved or time.time() >= deadline:
                    return black_out(img, area, self._private_rects(area)), th, moved
                time.sleep(0.1)
        except Exception:  # a failed capture must not hide what the steps did; assume it changed (no false alarm)
            return None, before, True

    def _focus_text(self) -> str:
        f = self.uia.focused()
        if not f:
            return ""
        return "focus " + (f'{f.role} "{_clip(f.name, 40)}"' if f.name else f.role)

    def act(self, steps: list[dict], observe: str = "diff") -> str:
        self.ensure_ready()
        if not isinstance(steps, list) or not steps:
            raise AgentError("steps must be a non-empty list")
        if elevation.secure_desktop():
            raise AgentError(elevation.SECURE_DESKTOP_MSG + " None of your steps were run.")
        brief = self._orient()
        if brief:
            return brief
        self._claim()
        prev_hwnd, prev = self.last.get("hwnd"), dict(self.last.get("by_ref", {}))
        if self.strict:
            ok, msg = self.gate.require({"kind": "batch", "summary": "run these steps"}, strict=True,
                                        detail="\n".join(_step_text(s) for s in steps)[:900])
            if not ok:
                return msg
        done = 0
        err = None
        notes: list[str] = []
        report: list[str] = []
        effects = moved_any = 0
        self.input.expect = None  # the user may use the mouse between calls; only watch it during this batch
        try:
            self._settle(self.areas[0], max_wait=0.6)  # baseline, and learns any area that never stops moving
            before = self._last_thumb
            for st in steps:
                tries = 0
                while True:
                    self._check_paused()
                    if tries == 0:
                        self.ui_log(_step_text(st))
                    portable = self._portable(st)
                    try:
                        self._typed_secret = False
                        note = self._step(st)
                        break
                    except UserInterrupted as e:
                        if not e.retry:
                            raise AgentError(f"{e}. Check the screen before trying again")
                        tries += 1
                        if tries > 3:
                            raise AgentError("the user keeps using the mouse; ask them before continuing")
                        waited = self._yield_to_user()
                        busy = "USER BUSY: " if waited >= 5 else ""
                        notes.append(f"note: {busy}the user took the mouse during step {done + 1}; waited "
                                     f"{waited:.0f}s until they let go, then redid that step. The screen may have "
                                     "changed.")
                img, before, moved = self._after_step(st, before)
                self._log_step(st, img=img)
                parts = [note] if note else []
                if _kind(st) in EFFECT_STEPS:
                    effects += 1
                    moved_any += moved
                    parts.append("screen changed" if moved else "NO visible change")
                if _kind(st) in FOCUS_STEPS:
                    parts.append(self._focus_text())
                report.append(f"{done + 1} {self._safe_step_text(st)}"
                              + (f" → {'; '.join(p for p in parts if p)}" if any(parts) else ""))
                done += 1
                self.actions += 1
                if self.recording is not None:
                    self.recording.append(portable)
        except UserBusy as e:
            err = (f"USER BUSY: {e}, so this step was not done. They are working on the screen right now: do not "
                   "fight them or retry in a loop. Wait, or ask the user; call act again with the remaining steps "
                   "when you continue (it waits for them to let go).")
        except (AgentError, ValueError, OSError, RuntimeError) as e:
            err = str(e)
        res = list(notes)
        if err:
            if done < len(steps):
                self._log_step(steps[done], ok=False, why=err)
            res.append(f"step {done + 1} failed: {err}" + (f" ({done} steps done)" if done else ""))
            self.ui_log(f"✗ {err[:60]}")
        elif effects and not moved_any:
            res.append(f"⚠ ran {done} steps, but the screen did NOT change after any of them: they probably had no "
                       "effect (focus elsewhere, app still loading, keys not reaching it). Check before continuing.")
        else:
            res.append(f"ok ({done} steps)")
        res += report
        if elevation.secure_desktop():  # e.g. a click on "Install" brought up a UAC prompt
            res.append(elevation.SECURE_DESKTOP_MSG)
        self.last_look = time.time()
        shot = None
        if observe != "none" and not self.pause_reason == "stopped":
            img, ms, settled = self._settle(self.areas[0], max_wait=2.0)
            if not settled:
                res.append(f"(screen still changing {ms} ms after the last step: loading or animating)")
            try:
                res.append(self._observe(prev_hwnd, prev, "diff" if observe == "screenshot" else observe))
            except AgentError as e:
                res.append(str(e))
            if observe == "screenshot":
                data, scale = encode_jpeg(img, 1280)
                self.shot = (self.areas[0].left, self.areas[0].top, scale)
                res.append(f"Screenshot below ({int(img.width * scale)}x{int(img.height * scale)}): click with "
                           "[x,y] in its pixels.")
                black = self._black_hint(img, self.areas[0])
                if black:
                    res.append(black)
                shot = data
        out = "\n".join(res)
        return (out, shot) if observe == "screenshot" else out

    def _portable(self, st: dict) -> dict:
        """Copy of a step with refs replaced by their visible names (refs don't survive sessions)."""
        out = dict(st)
        for k in R.TARGET_KEYS:
            v = out.get(k)
            if isinstance(v, str) and re.fullmatch(r"[eb]\d+", v):
                name = self._name_of(v)
                out[k] = name if name else v
        return out

    def _observe(self, prev_hwnd, prev: dict, mode: str) -> str:
        scope = self.last.get("scope", "window")
        snap = self.snapshot(scope if scope in ("window", "screen", "page") else "window")
        if mode == "full" or prev_hwnd != self.last["hwnd"] or not prev:
            return snap
        added, removed = diff(prev, self.last["by_ref"])
        if not added and not removed:
            return "no visible change"
        if len(added) > 40 and len(added) > 0.5 * max(1, len(self.last["by_ref"])):
            return "(page changed)\n" + snap
        out = [f"changes (+{len(added)} -{len(removed)}):"]
        out += [f"+ {a}" for a in added[:60]]
        if len(added) > 60:
            out.append(f"+ …{len(added) - 60} more")
        if removed:
            out.append("- " + ", ".join(r.split(' ', 1)[-1][:40] for r in removed[:10])
                       + (f", …{len(removed) - 10} more" if len(removed) > 10 else ""))
        return "\n".join(out)

    def _require(self, a: dict, detail: str = ""):
        ok, msg = self.gate.require(a, strict=False, detail=detail)
        audit({"event": "action", **{k: v for k, v in a.items() if k != "text"}, "allowed": ok})
        if not ok:
            raise AgentError(msg)

    def _step(self, st: dict) -> str:
        """Run one step. Returns a short note on what happened (or "")."""
        if not isinstance(st, dict):
            raise AgentError(f"each step must be an object, got {st!r}")
        if "paste" in st or "paste_file" in st:
            return self._paste(st)
        if "navigate" in st:
            return self._navigate(str(st["navigate"]), st.get("in"), bool(st.get("new_tab", False)))
        if "wait_stable" in st:
            return self._wait_stable(st["wait_stable"], float(st.get("timeout", 30)), float(st.get("quiet", 1.0)),
                                     bool(st.get("after_change", False)))
        if "wait_ocr" in st:
            return self._wait_ocr(str(st["wait_ocr"]), float(st.get("timeout", 30)), bool(st.get("gone", False)),
                                  st.get("in"))
        if "read" in st:
            text = self._read_text(self._region_rect(st["read"]))
            return f'read: "{_clip(text, 300)}"' if text else "read: (no text found)"
        return self._basic_step(st) or ""

    def _basic_step(self, st: dict):
        button = st.get("button", "left")
        for kind in ("click", "dblclick", "rclick", "hover"):
            if kind in st:
                x, y, name, info = self._resolve(st[kind])
                self._guard_point(x, y, admin_check=kind != "hover")  # the cursor itself reaches admin windows
                if kind == "hover":
                    self.input.move(x, y)
                    time.sleep(st.get("hold", 0.6))
                    return
                self._require({"kind": kind, "name": name, "role": info.get("role")})
                if kind == "rclick":
                    button = "right"
                self.input.click(x, y, button, 2 if kind == "dblclick" else st.get("count", 1))
                time.sleep(0.12)
                return
        if "type" in st:
            text = str(st["type"])
            rect = None
            if "into" in st:
                x, y, name, info = self._resolve(st["into"])
                self._guard_point(x, y)
                rect = info.get("rect") if info.get("role") != "ocr" else None  # an OCR hit is the label
                self.input.click(x, y)
                time.sleep(0.15)
            f = self._settled_focus()
            self._guard_keys()
            self._typed_secret = bool(f and f.password)
            self._require({"kind": "type", "password": bool(f and f.password), "name": f.name if f else ""})
            delay = _delay(st, 0.012)
            check = bool(st.get("verify")) and not self._typed_secret
            read = before = None
            if check:  # decide how to read the field, and what it held, before typing changes it
                read = self._field_reader(rect, clicked="into" in st)
                before = read()[0] if read and not st.get("clear") else None
            if st.get("clear"):
                self.input.press("ctrl+a")
                self.input.press("backspace")
            self.input.type_text(text, delay)
            note = ""
            if check:
                note = self._verify_typed(text, read, before, delay, bool(st.get("clear")), st.get("into"))
            if st.get("enter"):
                self.input.press("enter")
            return note
        if "key" in st:
            combo = normalize_combo(str(st["key"]))
            self._guard_keys()
            f = self.uia.focused()
            self._require({"kind": "key", "combo": combo, "editable": bool(f and f.role in EDITABLE),
                           "name": f.name if f else ""})
            for _ in range(int(st.get("repeat", 1))):
                self.input.press(combo)
            return
        if "key_down" in st:
            self._guard_keys()
            self.input.key_down(st["key_down"])
            return
        if "key_up" in st:
            self.input.key_up(st["key_up"])
            return
        if "scroll" in st:
            direction = st["scroll"]
            amount = int(st.get("amount", 5))
            if "at" in st:
                x, y, _, _ = self._resolve(st["at"])
            else:
                top = winutil.top_window(self.areas)
                x, y = top.center if top else (self.areas[0].left + self.areas[0].width // 2,
                                                self.areas[0].top + self.areas[0].height // 2)
            self._guard_point(x, y)
            dy = amount if direction == "down" else -amount if direction == "up" else 0
            dx = amount if direction == "right" else -amount if direction == "left" else 0
            self.input.scroll(x, y, dy, dx)
            return
        if "drag" in st:
            x1, y1, name, info = self._resolve(st["drag"])
            x2, y2, _, _ = self._resolve(st["to"])
            self._guard_point(x1, y1)
            self._guard_point(x2, y2)
            self._require({"kind": "drag", "name": name})
            self.input.drag(x1, y1, x2, y2, button)
            return
        if "wait" in st and isinstance(st["wait"], (int, float)) and not isinstance(st["wait"], bool):
            time.sleep(min(float(st["wait"]), 30))  # {"wait": 5} means "wait 5 seconds", not "wait for the text 5"
            return
        if "wait" in st:
            self._wait(str(st["wait"]), float(st.get("timeout", 10)), bool(st.get("gone", False)))
            return
        if "sleep" in st:
            time.sleep(min(float(st["sleep"]), 30))
            return
        if "focus" in st:
            self.window("focus", st["focus"])
            return
        if "launch" in st:
            self.launch(str(st["launch"]), observe=False, new_window=bool(st.get("new", False)))
            return
        raise AgentError(f"unknown step {st}")

    def _settled_focus(self, timeout: float = 1.5):
        """Wait until keyboard focus stops moving (window + element read twice the same), so the first typed
        characters don't land while an app is still opening a tab or moving focus."""
        end = time.time() + timeout
        prev = f = None
        while time.time() < end:
            f = self.uia.focused()
            key = (winutil.foreground(), (f.role, f.name, f.rect) if f else None)
            if f and key == prev:
                return f
            prev = key
            time.sleep(0.12)
        return f

    # ---- reading text back (canvas apps have no accessible values: fall back to OCR of the field)
    def _read_text(self, rect: Rect) -> str:
        return " | ".join(ln.text for ln in ocr(rect, self._grab_safe(rect)))

    def _field_reader(self, rect=None, clicked: bool = False):
        """Decide, BEFORE typing, how to read this field back: () -> (text or None, how).
        1. its accessible value (text boxes, and documents such as Notepad's; not a browser page, whose value is
           its URL); 2. OCR of the field's box; 3. OCR of a band around the text cursor (the caret), or around
           the point clicked by "into"; else None (can't tell where the text went)."""
        f = self.uia.focused()
        if f and f.value is not None and not f.password and (
                f.role in ("edit", "combo") or (f.role == "doc" and not re.match(r"^[a-z][a-z0-9+.-]*:", str(f.value)))):
            def by_value():
                g = self.uia.focused()
                return (str(g.value) if g and g.value is not None else None), "field value"
            return by_value
        area = self.areas[0]
        big = lambda r: (r[2] - r[0]) * (r[3] - r[1]) > 0.25 * area.width * area.height  # noqa: E731
        if (not rect or big(rect)) and f and f.rect and f.rect[2] > f.rect[0] and not big(f.rect):
            rect = f.rect
        if not rect or big(rect):
            caret = winutil.caret_rect()
            if caret:
                rect = (caret[0] - 260, caret[1] - 6, caret[0] + 260, caret[3] + 6)
            elif clicked:  # a canvas app: the 'element' is the whole canvas; read around where we clicked
                from .act import cursor_pos

                x, y = cursor_pos()
                rect = (x - 200, y - 24, x + 200, y + 24)
            else:
                return None
        l, t, r, b = rect
        l, t, r, b = max(area.left, l - 4), max(area.top, t - 4), min(area.right, r + 4), min(area.bottom, b + 4)
        if r - l < 4 or b - t < 4:
            return None
        box = Rect(l, t, r - l, b - t)
        return lambda: (self._read_text(box), "OCR")

    @staticmethod
    def _was_added(before: str | None, after: str, text: str, clear: bool) -> bool:
        """Did typing put `text` into the field? Not 'is the field equal to it': typing usually adds to text that
        is already there. Counted, so an X typed into a field that already had an X still has to add one."""
        w, a = norm_text(text), norm_text(after)
        if not w:
            return True
        if clear or before is None:
            return w in a
        return a.count(w) > norm_text(before).count(w)

    def _verify_typed(self, text: str, read, before, delay: float, clear: bool, into) -> str:
        """Read the field back; if keys were dropped (canvas apps redraw while keys arrive), retype slower."""
        if "\n" in text or len(text) > 200:
            return "verify skipped (multi-line or long text)"
        if read is None:
            return ("could not verify: no readable field and no text cursor found (give \"into\", or check with "
                    "a screenshot)")
        want = norm_text(text)
        for attempt in range(3):
            time.sleep(0.3)
            got, how = read()
            if got is None:
                return "could not read the field back to verify (take a screenshot to check)"
            if self._was_added(before, got, text, clear):
                return f"verified ({how})" + (f" after {attempt} slower retype(s)" if attempt else "")
            # Retype only when nothing of the user's can be lost: "clear" was asked, or the field was empty.
            empty_before = not norm_text(before or "")
            if attempt == 2 or not (clear or (empty_before and norm_text(got) in want)):
                return (f'⚠ VERIFY FAILED: "{_clip(text, 40)}" did not appear in the field; it shows '
                        f'"{_clip(got, 60)}"' + ("" if clear else " (\"clear\": true lets DeskPilot retype it)"))
            if into is not None:
                x, y, _, _ = self._resolve(into)
                self.input.click(x, y)
                time.sleep(0.15)
            self.input.press("ctrl+a")
            self.input.press("backspace")
            delay = max(delay * 3, 0.06)
            self.input.type_text(text, delay)
        return ""

    # ---- paste without losing the user's clipboard
    def _paste(self, st: dict) -> str:
        if "paste_file" in st:
            path = str(st["paste_file"])
            text = clip.read_file(path)
            src = f" from {os.path.basename(path)}"
        else:
            text, src = str(st["paste"]), ""
        if "into" in st:
            x, y, _, _ = self._resolve(st["into"])
            self._guard_point(x, y)
            self.input.click(x, y)
            time.sleep(0.15)
        f = self._settled_focus()
        self._guard_keys()
        self._typed_secret = bool(f and f.password)
        self._require({"kind": "type", "password": bool(f and f.password), "name": f.name if f else ""})
        if st.get("clear"):
            self.input.press("ctrl+a")  # the paste replaces the selection
        area = self.areas[0]
        before = thumb(grab(area))
        saved, lost = clip.save()
        try:
            clip.set_text(text, private=True)
            self.input.press("ctrl+v")
            # Restore only once the app has taken the text (the screen changes), so it can't paste the old content.
            end = time.time() + min(3.0, 1.0 + len(text) / 20000)
            while time.time() < end and not changed(before, thumb(grab(area)), self._moving):
                time.sleep(0.1)
            time.sleep(0.25)
        finally:
            clip.restore(saved)
        if st.get("enter"):
            self.input.press("enter")
        note = f"pasted {len(text)} chars{src}"
        f2 = self.uia.focused()
        if f2 and f2.role in ("edit", "combo") and f2.value not in (None, "") and not f2.password:
            note += f"; field now holds {len(str(f2.value))} chars"
        note += "; your clipboard was put back" + (
            f" (except {lost} item(s) that can't be copied, e.g. files copied in Explorer)" if lost else "")
        return note

    # ---- browser address bar, without Ctrl+L guesswork or the clipboard
    def _browser_window(self, where=None) -> winutil.Win:
        if where is not None:
            w = self._window_target(where)
        else:
            w = next((w for w in winutil.windows() if winutil.on_area(w, self.areas)
                      and w.exe().lower() in BROWSER_EXES), None)
            if not w:
                raise AgentError("no browser window on your screen; use launch(url) to open one")
        if w.exe().lower() not in BROWSER_EXES:
            raise AgentError(f'"{w.title[:40]}" is not a browser window')
        if self._private(w):
            raise AgentError("that window is private (privacy settings)")
        return w

    def _bar_focused(self, info) -> bool:
        f = self.uia.focused()
        if not f:
            return False
        return (bool(info) and tuple(f.rect) == tuple(info[0])) or f.name in UIA.ADDRESS_BAR_NAMES

    def _navigate(self, url: str, where=None, new_tab: bool = False) -> str:
        url = url.strip()
        if not url:
            raise AgentError("navigate needs a URL")
        w = self._browser_window(where)
        if winutil.foreground() != w.hwnd or not winutil.on_area(w, self.areas):
            self._bring_here(w)
        if new_tab:
            self.input.press("ctrl+t")
            time.sleep(0.4)
        info = self.uia.address_bar_info(w.hwnd)
        for how in ("ctrl+l", "click", "alt+d", "f6"):
            if how == "click":
                if not info:
                    continue
                l, t, r, b = info[0]
                x, y = (l + r) // 2, (t + b) // 2
                self._guard_point(x, y)
                self.input.click(x, y)
            else:
                self.input.press(how)
            time.sleep(0.15)
            if self._bar_focused(info):
                break
        else:
            raise AgentError("could not put the cursor in the browser's address bar; take a screenshot to see why")
        self.input.press("ctrl+a")
        self.input.type_text(url, 0.008)
        self.input.press("delete")  # drop Chrome's inline autocomplete, so Enter opens exactly this URL
        self.input.press("enter")
        shown = ""
        end = time.time() + 8
        while time.time() < end:
            time.sleep(0.3)
            i2 = self.uia.address_bar_info(w.hwnd)
            shown = i2[1] if i2 else ""
            if shown and not self._bar_focused(i2):
                break
        return f"address bar shows {_clip(shown, 100)}" if shown else "sent the URL (address bar not readable)"

    # ---- waiting for apps that have no readable text
    def _wait_stable(self, spec, timeout: float, quiet: float, after_change: bool) -> str:
        rect = self._region_rect(None if spec is True else spec)
        start = last_change = time.time()
        prev = thumb(grab(rect))
        seen = False
        while True:
            self._check_paused()
            time.sleep(0.25)
            cur = thumb(grab(rect))
            now = time.time()
            if changed(prev, cur):
                last_change, seen, prev = now, True, cur
            if now - last_change >= quiet and (seen or not after_change):
                return f"stable after {now - start:.1f}s" + ("" if seen else " (it did not change at all)")
            if now - start > timeout:
                why = "it never changed" if after_change and not seen else "it kept changing"
                raise AgentError(f"wait_stable timed out after {timeout:g}s: {why}")

    def _wait_ocr(self, text: str, timeout: float, gone: bool, where=None) -> str:
        areas = [self._region_rect(where)] if where is not None else self.areas
        start = time.time()
        while True:
            self._check_paused()
            found = any(ocr_match(ocr(a, self._grab_safe(a)), text) for a in areas)
            waited = time.time() - start
            if found != gone:
                return f'"{_clip(text, 40)}" {"gone" if gone else "on screen"} after {waited:.1f}s'
            if waited > timeout:
                raise AgentError(f'timed out after {timeout:g}s waiting for "{text}" '
                                 f'{"to disappear" if gone else "to appear"} (OCR)')
            time.sleep(0.5)

    def _wait(self, text: str, timeout: float, gone: bool):
        end = time.time() + timeout
        while time.time() < end:
            self._check_paused()
            titles = " ".join(w.title for w in winutil.windows() if winutil.on_area(w, self.areas))
            present = text.lower() in titles.lower() or self._present(text)
            if present != gone:
                return
            time.sleep(0.4)
        raise AgentError(f'timed out after {timeout:g}s waiting for "{text}"{" to disappear" if gone else ""}')

    def _present(self, text) -> bool:
        w = winutil.top_window(self.areas)
        if not w:
            return False
        page = self._page(w)
        try:
            if page:
                return bool(self.browsers.extract(page, True, text, 1)["items"])
            # count text that is on the page but scrolled out of view too: "wait" means "has it loaded?"
            return bool(find_nodes(self.uia.tree(w.hwnd), text, None, self.areas, onscreen=False))
        except Exception:
            return False

    # ------------------------------------------------------------ routines
    def routine(self, op: str, name: str | None = None, steps: list | None = None, params: dict | None = None,
                description: str = "") -> str:
        if op == "list":
            rs = R.all_routines()
            if not rs:
                return "no saved routines"
            return "\n".join(f'{r["name"]}({", ".join(r["params"])}) - {r.get("description", "")} '
                             f'[{r.get("runs", 0)} runs]' for r in rs)
        if op == "start":
            self.recording = []
            return "recording: do the task with act/launch, then routine(op='save', name=..., description=...)"
        if not name:
            raise AgentError("name is required")
        if op == "save":
            if steps:
                data = [self._portable(s) for s in steps]
            elif self.recording:
                data = self.recording
            else:
                raise AgentError("nothing to save: pass steps, or routine(op='start') then do the task first")
            bad = [s for s in data if any(isinstance(s.get(k), list) for k in R.TARGET_KEYS)]
            d = R.save(name, data, description)
            self.recording = None
            note = "  (warning: some steps use [x,y] pixels, which may break if the layout changes)" if bad else ""
            return f'saved routine "{name}" ({len(data)} steps, params: {", ".join(d["params"]) or "none"}){note}'
        if op == "show":
            d = R.load(name)
            return json.dumps(d["steps"], ensure_ascii=False)
        if op == "delete":
            return "deleted" if R.delete(name) else "not found"
        if op == "run":
            d = R.load(name)
            filled = R.fill(d["steps"], params or {})
            self.ui_log(f"▶ routine {name}")
            res = self.act(filled)
            if res.startswith("ok"):
                R.bump(name)
            return f"routine {name}: {res}"
        raise AgentError(f"unknown routine op {op}")

    # ------------------------------------------------------------ apps / windows / clipboard
    def _start_apps(self) -> list[dict]:
        if self._apps is None:
            try:
                out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                      "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"],
                                     capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW).stdout
                data = json.loads(out or "[]")
                self._apps = data if isinstance(data, list) else [data]
            except Exception:
                self._apps = []
        return self._apps

    # ---- "already open?" checks, so launch() switches instead of opening a second copy
    def _bring_here(self, w: winutil.Win) -> None:
        if w.minimized:
            winutil.focus(w.hwnd)
        if not winutil.on_area(w, self.areas):
            winutil.move_to(w.hwnd, self.areas[0])
        winutil.focus(w.hwnd)
        time.sleep(0.3)

    def _reuse_tab(self, url: str) -> str | None:
        """If a browser tab already shows this page, switch to it and return a message."""
        from .browser import same_page

        wins = [w for w in winutil.windows(include_minimized=True) if w.exe().lower() in BROWSER_EXES]
        if not wins:
            return None
        # 1. A window already showing it (address bar via accessibility; no permission prompt, any browser).
        for w in wins:
            shown = self.uia.address_bar(w.hwnd)
            if shown and same_page(url, shown if "://" in shown else "https://" + shown):
                self._bring_here(w)
                return f'already open: {self.wref(w.hwnd)} "{w.title[:60]}" is showing {shown[:80]}'
        # 2. A background tab, over browser connections that are already allowed.
        hit = self.browsers.find_tab(url, wins)
        if not hit:
            return None
        c, tab = hit
        try:
            c.call("Target.activateTarget", {"targetId": tab["targetId"]})
        except Exception:
            return None
        title = tab.get("title") or ""
        end = time.time() + 2
        while time.time() < end:  # the window title follows the active tab
            w = next((w for w in winutil.windows(include_minimized=True)
                      if w.exe().lower() in BROWSER_EXES and title and w.title.startswith(title[:40])), None)
            if w:
                self._bring_here(w)
                return f'already open: switched to the existing tab "{title[:60]}" ({tab["url"][:80]}) -> ' \
                       f'{self.wref(w.hwnd)}'
            time.sleep(0.15)
        return None

    def _reuse_app(self, name: str) -> str | None:
        """If a window of this app is already open, bring it here and return a message."""
        q = name.lower().strip()
        compact = q.replace(" ", "")
        for w in winutil.windows(include_minimized=True):
            title = w.title.lower()
            stem = w.exe().lower().removesuffix(".exe")
            if title == q or title.endswith(" - " + q) or title.startswith(q + " - ") or stem == compact:
                self._bring_here(w)
                return f'already open: switched to {self.wref(w.hwnd)} "{w.title[:60]}"'
        return None

    def launch(self, target: str, observe: bool = True, new_window: bool = False) -> str:
        self.ensure_ready()
        self._claim()
        self.timeline.record(f"launch {target[:80]}")
        t = target.strip()
        self.ui_log(f"launch {t[:50]}")
        before = {w.hwnd for w in winutil.windows(include_minimized=True)}
        fg_before = winutil.foreground()
        how = ""
        m = re.match(r"^(agent|deskpilot) browser\s*(.*)$", t, re.I)
        web = (m.group(2).strip() if m else t)
        if not new_window and re.match(r"^(https?://|www\.)", web, re.I):
            reused = self._reuse_tab(web)
            if reused:
                audit({"event": "launch", "target": t, "reused": True})
                return reused + ("\n" + self.snapshot() if observe else "")
        if m:
            url = m.group(2).strip() or "about:blank"
            if not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
                url = "https://" + url
            launch_agent_browser(url)
            how = "opened DeskPilot browser"
        elif re.match(r"^[a-z][a-z0-9+.-]*:", t, re.I) and not re.match(r"^[a-z]:\\", t, re.I) or t.startswith("www."):
            if t.startswith("www."):
                t = "https://" + t
            os.startfile(t)
            how = "opened"
        elif os.path.exists(os.path.expandvars(t)):
            p = os.path.expandvars(t)
            self._require({"kind": "launch", "app": p})
            os.startfile(p)
            how = "opened"
        else:
            self._require({"kind": "launch", "app": t})
            if not new_window:
                reused = self._reuse_app(t)
                if reused:
                    audit({"event": "launch", "target": t, "reused": True})
                    return reused + ("\n" + self.snapshot() if observe else "")
            apps = self._start_apps()
            q = t.lower()
            app = (next((a for a in apps if a["Name"].lower() == q), None)
                   or next((a for a in apps if a["Name"].lower().startswith(q)), None)
                   or next((a for a in apps if q in a["Name"].lower()), None))
            if app:
                subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app['AppID']}"], creationflags=NO_WINDOW)
                how = f"launched {app['Name']}"
            elif shutil.which(t):
                subprocess.Popen([shutil.which(t)], creationflags=NO_WINDOW)
                how = f"launched {t}"
            else:
                close = [a["Name"] for a in apps if any(p in a["Name"].lower() for p in q.split())][:8]
                raise AgentError(f'app "{t}" not found' + (f"; similar: {', '.join(close)}" if close else ""))
        audit({"event": "launch", "target": t})
        if self.recording is not None and observe:
            self.recording.append({"launch": t})
        w = self._wait_new_window(before, fg_before)
        if w and not winutil.on_area(w, self.areas):
            winutil.move_to(w.hwnd, self.areas[0])
        if w:
            winutil.focus(w.hwnd)
            _wait_input_idle(w.hwnd)
            time.sleep(0.8)  # packaged apps (Notepad, Store...) keep loading after input-idle
        msg = f"{how}: {t}" + (f" -> {self.wref(w.hwnd)} \"{w.title[:60]}\"" if w else " (no new window seen yet)")
        return msg + ("\n" + self.snapshot() if observe else "")

    def _wait_new_window(self, before: set[int], fg_before: int, timeout: float = 10):
        end = time.time() + timeout
        while time.time() < end:
            wins = winutil.windows()
            new = [w for w in wins if w.hwnd not in before]
            if new:
                time.sleep(0.5)
                return next((w for w in winutil.windows() if w.hwnd == new[0].hwnd), new[0])
            fg = winutil.foreground()
            if fg != fg_before and time.time() > end - timeout + 1.5:
                w = next((w for w in wins if w.hwnd == fg), None)
                if w:
                    return w
            time.sleep(0.25)
        return None

    def _window_target(self, target) -> winutil.Win:
        wins = winutil.windows(include_minimized=True)
        if target is None:
            w = winutil.top_window(self.areas)
            if not w:
                raise AgentError("no window on the controlled screen")
            return w
        t = str(target)
        if re.fullmatch(r"w\d+", t) and t in self.win_by_ref:
            h = self.win_by_ref[t]
            w = next((w for w in wins if w.hwnd == h), None)
            if w:
                return w
            raise AgentError(f"window {t} no longer exists")
        w = next((w for w in wins if t.lower() in w.title.lower()), None) or \
            next((w for w in wins if t.lower() in w.exe().lower()), None)
        if not w:
            raise AgentError(f'no window matching "{t}"')
        return w

    def window(self, op: str = "list", target: str | None = None) -> str:
        self.ensure_ready()
        if op == "list":
            mons = monitors()
            out = []
            for w in winutil.windows(include_minimized=True)[:30]:
                cx, cy = w.center
                scr = next((i + 1 for i, m in enumerate(mons) if m.contains(cx, cy)), "?")
                mark = "" if winutil.on_area(w, self.areas) or w.minimized else " (other screen)"
                title = "(private)" if self._private(w) else f'"{w.title[:60]}"'
                admin = " admin" if elevation.window_above_us(w.hwnd) else ""
                out.append(f'{self.wref(w.hwnd)} {title} {w.exe()}{admin} screen{scr}'
                           f'{" minimized" if w.minimized else ""}{mark}')
            return "\n".join(out) or "no windows"
        self._claim()
        self.timeline.record(f"window {op} {target or ''}".strip())
        w = self._window_target(target)
        if self._private(w):
            raise AgentError("that window is private (privacy settings)")
        self.ui_log(f"{op} window {w.title[:40]}")
        if op != "focus":
            self._admin_blocked(w.hwnd)  # bringing it to the front works; closing, moving and resizing don't
        if op == "close":
            self._require({"kind": "close", "window": w.title[:60]})
            winutil.close(w.hwnd)
        elif op in ("focus", "move_here"):
            if op == "move_here" or not winutil.on_area(w, self.areas):
                winutil.move_to(w.hwnd, self.areas[0])
            winutil.focus(w.hwnd)
        elif op in ("minimize", "maximize", "restore"):
            if not winutil.on_area(w, self.areas) and op != "minimize":
                winutil.move_to(w.hwnd, self.areas[0], maximize=op == "maximize")
            winutil.set_state(w.hwnd, op)
        else:
            raise AgentError(f"unknown window op {op}")
        audit({"event": "window", "op": op, "title": w.title[:80]})
        time.sleep(0.3)
        return f'{op} {self.wref(w.hwnd)} "{w.title[:60]}" done'

    def clipboard(self, op: str = "get", text: str | None = None, file: str | None = None) -> str:
        try:
            if op == "set":
                self._claim()
                if file:
                    text = clip.read_file(file)
                if self._user_clip is None:  # first set of this task: keep what the user had copied
                    self._user_clip = clip.save()[0]
                clip.set_text(text or "", private=True)
                self._our_clip = text or ""
                return (f"clipboard set ({len(text or '')} chars{' from ' + os.path.basename(file) if file else ''}). "
                        "The user's own clipboard is saved: it is put back at done(), or now with "
                        "clipboard(op='restore'). For pasting, act's paste/paste_file steps are simpler.")
            if op == "restore":
                return self._restore_user_clip(force=True)
            data = clip.get_text()
        except (OSError, ValueError, RuntimeError) as e:
            raise AgentError(str(e))
        if data is None:
            return "(clipboard has no text)"
        return data if len(data) < 6000 else data[:6000] + f"…(+{len(data) - 6000} chars)"

    def _restore_user_clip(self, force: bool = False) -> str:
        """Put back what the user had copied before clipboard('set'). At the end of a task this only happens
        while the clipboard still holds the agent's text: if the user copied something new meanwhile, keep it."""
        if self._user_clip is None:
            return "nothing to restore: the clipboard was not changed with clipboard('set') this task"
        try:
            if not force and clip.get_text() != self._our_clip:
                self._user_clip = self._our_clip = None
                return ""
            clip.restore(self._user_clip)
        except RuntimeError as e:
            return f"could not restore the clipboard: {e}"
        self._user_clip = self._our_clip = None
        return "the user's clipboard was put back"

    def switch_screen(self, screen: str) -> str:
        ok, msg = self.gate.require({"kind": "screen", "name": f"screen {screen}"})
        if not ok:
            return msg
        self.session.monitor = "all" if screen == "all" else int(screen) - 1
        self.session.save()
        self.shot = None
        self.overlay.start(self.areas, self.session.mode)
        return self.info()


def _wait_input_idle(hwnd: int, timeout_ms: int = 3000) -> None:
    """Block until the window's process is ready for keyboard input (so the first keys aren't lost)."""
    try:
        import ctypes

        import win32process

        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x0400 | 0x00100000, False, pid)  # QUERY_INFORMATION | SYNCHRONIZE
        if h:
            try:
                ctypes.windll.user32.WaitForInputIdle(h, timeout_ms)
            finally:
                k32.CloseHandle(h)
    except Exception:
        pass


STEP_KINDS = ("click", "dblclick", "rclick", "hover", "type", "paste", "paste_file", "navigate", "key", "key_down",
              "key_up", "scroll", "drag", "wait_stable", "wait_ocr", "wait", "sleep", "read", "focus", "launch")
# Steps that should visibly change something (the batch warns when none of them did), and the ones where
# keyboard focus matters (the report says where focus ended up).
EFFECT_STEPS = {"click", "dblclick", "rclick", "type", "paste", "paste_file", "navigate", "key", "scroll", "drag"}
FOCUS_STEPS = {"click", "dblclick", "type", "paste", "paste_file", "key", "navigate"}


def _kind(st) -> str:
    if not isinstance(st, dict):
        return ""
    return next((k for k in STEP_KINDS if k in st), next(iter(st), ""))


def _delay(st: dict, default: float) -> float:
    """Seconds between typed characters: "delay_ms" (0-1000) or the default."""
    try:
        return max(0.0, min(float(st["delay_ms"]), 1000.0)) / 1000 if st.get("delay_ms") is not None else default
    except (TypeError, ValueError):
        return default


def _step_text(st) -> str:
    if not isinstance(st, dict) or not st:
        return str(st)[:60]
    k = _kind(st)
    v = st.get(k)
    into = f" into {json.dumps(st['into'], ensure_ascii=False)[:40]}" if "into" in st else ""
    if k == "type":
        v = (str(v)[:30] + "…") if len(str(v)) > 30 else v
        return f'type "{v}"{into}'
    if k == "paste":
        return f"paste {len(str(v))} chars{into}"
    if k == "paste_file":
        return f"paste file {os.path.basename(str(v))}{into}"
    return f"{k} {json.dumps(v, ensure_ascii=False)[:50]}"
