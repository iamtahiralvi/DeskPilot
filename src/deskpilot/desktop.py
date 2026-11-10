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

from . import routines as R
from . import winutil
from .act import Input, UserInterrupted
from .browser import BROWSER_EXES, Browsers, launch_agent_browser
from .config import Rect, Session, monitors
from .perceive import (EDITABLE, UIA, RefTable, describe, diff, draw_marks, encode_jpeg, find_nodes, format_tree,
                       grab, ocr, ocr_match, _clip)
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
                self.proc = subprocess.Popen([sys.executable, "-m", "deskpilot.overlay"], stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, text=True, bufsize=1, creationflags=NO_WINDOW)
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
        self.browsers.allow_real_profile = bool(self.gate.policy.rules.get("use_my_browser", True))
        self.win_refs: dict[int, str] = {}
        self.win_by_ref: dict[str, int] = {}
        self.last: dict = {"hwnd": None, "by_ref": {}}
        self.shot = None  # (left, top, scale) of the last screenshot
        self.pause_reason = ""
        self.recording: list | None = None
        self.tokens = 0
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

    # ------------------------------------------------------------ overlay lifetime
    def busy(self):
        """A tool call started: keep the overlay up."""
        if self._idle_timer:
            self._idle_timer.cancel()
            self._idle_timer = None

    def idle(self):
        """A tool call ended: hide the overlay if the agent stays quiet (it may have finished without done())."""
        self.busy()
        if self.idle_hide > 0:
            self._idle_timer = threading.Timer(self.idle_hide, self.finish, kwargs={"reason": "idle"})
            self._idle_timer.daemon = True
            self._idle_timer.start()

    def finish(self, summary: str = "", reason: str = "done") -> str:
        """Task over: drop the overlay. A user Pause/Stop stays in effect (the next call shows it again);
        an automatic 'you moved the mouse' pause is cleared so the next task starts clean."""
        if self.pause_reason == "moved":
            self.pause_reason = ""
            self.overlay.send({"op": "paused", "v": ""})
        if summary:
            self.ui_log("✓ " + summary)
        self.ui_state("idle")
        self.overlay.hide()
        audit({"event": "finish", "reason": reason, **({"summary": summary[:200]} if summary else {})})
        return "Overlay hidden. Tell the user what you did." + (
            f" Note: the user has you {self.pause_reason.upper()}; they unlock it from the panel next time."
            if self.paused else "")

    # ------------------------------------------------------------ plumbing
    def _init_thread(self):
        self.uia = UIA()

    def call(self, fn, *a, **kw):
        return self.pool.submit(fn, *a, **kw)

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
        self.overlay.send({"op": "stats", "tokens": self.tokens, "actions": self.actions})

    def _check_paused(self):
        if self.paused:
            raise AgentError(PAUSE_MSG[self.pause_reason])

    def ensure_ready(self):
        if not self.session.picked:
            self.pick()
        self.overlay.start(self.session.areas(), self.session.mode)
        self._check_paused()

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
            lines.append(f"top window: {self.wref(top.hwnd)} {title}")
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
        try:
            tree = self.uia.tree(w.hwnd)
        except Exception as e:
            return [f"(cannot read window: {e.__class__.__name__}; try scope='ocr')"], {}
        lines, by_ref, hidden = format_tree(tree, self.areas, self.refs, max_nodes, filt)
        if hidden:
            lines.append(f"…{hidden} more elements hidden (use find, or snapshot with filter)")
        if not lines:
            lines.append("(no accessible elements; use scope='ocr' or screenshot)")
        return lines, by_ref

    def snapshot(self, scope: str = "window", filter: str | None = None, max_nodes: int = 250) -> str:
        self.ensure_ready()
        if scope == "ocr":
            return self._ocr_snapshot(filter)
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

    def _ocr_snapshot(self, filt: str | None) -> str:
        out = [f"{self._screen_label()} (OCR text; refs are clickable)"]
        for a in self.areas:
            for ln in ocr(a, self._grab_safe(a)):
                if filt and filt.lower() not in ln.text.lower():
                    continue
                ref = self.refs.assign("ocr", ln.text, "", ln.rect)
                out.append(f'{ref} "{ln.text[:100]}"')
        return "\n".join(out) if len(out) > 1 else out[0] + "\n(no text found)"

    def find(self, text: str | None = None, role: str | None = None, limit: int = 15) -> str:
        self.ensure_ready()
        hits = self._find(text, role, limit)
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
            rect = self._ref_rect(ref)
            l, t, r, b = rect
            pad = 40
            nl, nt = max(area.left, l - pad), max(area.top, t - pad)
            area = Rect(nl, nt, max(10, min(r + pad, area.right) - nl), max(10, min(b + pad, area.bottom) - nt))
        elif region:
            x, y, w, h = region
            ox, oy, sc = self.shot or (area.left, area.top, 1.0)
            area = Rect(int(ox + x / sc), int(oy + y / sc), int(w / sc), int(h / sc))
        img = self._grab_safe(area)
        if marks:
            infos = [self.refs.get(r) for r in self.last.get("by_ref", {}) if r.startswith("e")]
            infos += [type("I", (), {"ref": r, **self.web[r]}) for r in self.last.get("by_ref", {}) if r in self.web]
            draw_marks(img, area, [i for i in infos if i])
        data, scale = encode_jpeg(img, max_side)
        self.shot = (area.left, area.top, scale)
        note = f"{int(img.width * scale)}x{int(img.height * scale)} image; [x,y] targets use image pixels"
        return data, note

    # ------------------------------------------------------------ targets
    def _ref_rect(self, ref: str):
        if ref in self.web:
            return self.web[ref]["rect"]
        info = self.refs.get(ref)
        if not info:
            raise AgentError(f"unknown ref {ref}; take a new snapshot")
        return info.rect

    def _locate_web(self, ref: str) -> tuple[int, int]:
        info = self.web[ref]
        try:
            r = self.browsers.locate(info["page"], ref)
        except Exception as e:
            raise AgentError(f"page changed ({str(e)[:60]}); take a new snapshot")
        if not r:
            raise AgentError(f"{ref} is gone from the page; take a new snapshot")
        cr = self._content_rect(info["hwnd"])
        if not cr:
            x, y = ((info["rect"][0] + info["rect"][2]) // 2, (info["rect"][1] + info["rect"][3]) // 2)
            return x, y
        scale = (cr[2] - cr[0]) / max(1, r["vw"])
        cx = r["x"] + min(r["w"] / 2, max(r["w"], 1) - 1)
        cy = r["y"] + r["h"] / 2
        time.sleep(0.05)
        return int(cr[0] + cx * scale), int(cr[1] + cy * scale)

    def _resolve(self, target) -> tuple[int, int, str, dict]:
        """target -> (x, y, name, info). Accepts ref 'e12'/'b12', [x, y] image/screen px, or visible text."""
        if isinstance(target, (list, tuple)) and len(target) == 2:
            ox, oy, sc = self.shot or (self.areas[0].left, self.areas[0].top, 1.0)
            x, y = int(ox + target[0] / sc), int(oy + target[1] / sc)
            n = self.uia.at_point(x, y)
            return x, y, (n.name if n else ""), {"role": n.role if n else ""}
        if isinstance(target, str):
            t = target.strip()
            if re.fullmatch(r"b\d+", t):
                if t not in self.web:
                    raise AgentError(f"unknown ref {t}; take a new snapshot")
                x, y = self._locate_web(t)
                i = self.web[t]
                return x, y, i["name"], {"role": i["role"], "editable": i["editable"], "password": i["password"]}
            if re.fullmatch(r"e\d+", t):
                info = self.refs.get(t)
                if not info:
                    raise AgentError(f"unknown ref {t}; take a new snapshot")
                x, y = info.center
                return x, y, info.name, {"role": info.role, "editable": info.editable, "password": info.password}
            hits = self._find(t, None, 1)
            if hits:
                return self._resolve(hits[0].split()[0])
            raise AgentError(f'no element with text "{t}" on screen')
        raise AgentError(f"bad target {target!r}")

    def _name_of(self, ref: str) -> str:
        if ref in self.web:
            return self.web[ref]["name"]
        info = self.refs.get(ref)
        return info.name if info else ""

    def _guard_point(self, x, y):
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

    # ------------------------------------------------------------ actions
    def act(self, steps: list[dict], observe: str = "diff") -> str:
        self.ensure_ready()
        if not isinstance(steps, list) or not steps:
            raise AgentError("steps must be a non-empty list")
        prev_hwnd, prev = self.last.get("hwnd"), dict(self.last.get("by_ref", {}))
        if self.strict:
            ok, msg = self.gate.require({"kind": "batch", "summary": "run these steps"}, strict=True,
                                        detail="\n".join(_step_text(s) for s in steps)[:900])
            if not ok:
                return msg
        done = 0
        err = None
        try:
            for st in steps:
                self._check_paused()
                self.ui_log(_step_text(st))
                portable = self._portable(st)
                self._step(st)
                done += 1
                self.actions += 1
                if self.recording is not None:
                    self.recording.append(portable)
        except UserInterrupted as e:
            self._set_pause("moved")
            err = str(e)
        except (AgentError, ValueError) as e:
            err = str(e)
        res = []
        if err:
            res.append(f"step {done + 1} failed: {err}" + (f" ({done} steps done)" if done else ""))
            self.ui_log(f"✗ {err[:60]}")
        else:
            res.append(f"ok ({done} steps)")
        if observe != "none" and not self.pause_reason == "stopped":
            time.sleep(0.35)
            try:
                res.append(self._observe(prev_hwnd, prev, observe))
            except AgentError as e:
                res.append(str(e))
        return "\n".join(res)

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

    def _step(self, st: dict):
        if not isinstance(st, dict):
            raise AgentError(f"each step must be an object, got {st!r}")
        button = st.get("button", "left")
        for kind in ("click", "dblclick", "rclick", "hover"):
            if kind in st:
                x, y, name, info = self._resolve(st[kind])
                self._guard_point(x, y)
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
            if "into" in st:
                x, y, name, info = self._resolve(st["into"])
                self._guard_point(x, y)
                self.input.click(x, y)
                time.sleep(0.15)
            f = self._settled_focus()
            self._require({"kind": "type", "password": bool(f and f.password), "name": f.name if f else ""})
            if st.get("clear"):
                self.input.press("ctrl+a")
                self.input.press("backspace")
            self.input.type_text(text)
            if st.get("enter"):
                self.input.press("enter")
            return
        if "key" in st:
            combo = normalize_combo(str(st["key"]))
            f = self.uia.focused()
            self._require({"kind": "key", "combo": combo, "editable": bool(f and f.role in EDITABLE),
                           "name": f.name if f else ""})
            for _ in range(int(st.get("repeat", 1))):
                self.input.press(combo)
            return
        if "key_down" in st:
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
            return bool(find_nodes(self.uia.tree(w.hwnd), text, None, self.areas))
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
                out.append(f'{self.wref(w.hwnd)} {title} {w.exe()} screen{scr}'
                           f'{" minimized" if w.minimized else ""}{mark}')
            return "\n".join(out) or "no windows"
        w = self._window_target(target)
        if self._private(w):
            raise AgentError("that window is private (privacy settings)")
        self.ui_log(f"{op} window {w.title[:40]}")
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

    def clipboard(self, op: str = "get", text: str | None = None) -> str:
        import win32clipboard as cb

        for _ in range(5):
            try:
                cb.OpenClipboard()
                break
            except Exception:
                time.sleep(0.1)
        try:
            if op == "set":
                cb.EmptyClipboard()
                cb.SetClipboardText(text or "", cb.CF_UNICODETEXT)
                return f"clipboard set ({len(text or '')} chars)"
            try:
                data = cb.GetClipboardData(cb.CF_UNICODETEXT)
            except Exception:
                return "(clipboard has no text)"
            return data if len(data) < 6000 else data[:6000] + f"…(+{len(data) - 6000} chars)"
        finally:
            cb.CloseClipboard()

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


def _step_text(st) -> str:
    if not isinstance(st, dict) or not st:
        return str(st)[:60]
    k = next(iter(st))
    v = st[k]
    if k == "type":
        v = (str(v)[:30] + "…") if len(str(v)) > 30 else v
        return f'type "{v}"' + (f" into {st['into']}" if "into" in st else "")
    return f"{k} {json.dumps(v, ensure_ascii=False)[:50]}"
