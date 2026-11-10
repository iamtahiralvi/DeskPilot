"""Overlay (agent cursor, click ripples, screen border, status chip) + floating control panel.

Runs as its own process (`python -m deskpilot.overlay`). JSON commands on stdin, one per line:
  areas{rects,mode} move{x,y} click{x,y} label{text} state{v,text} log{text} stats{tokens,actions}
  paused{v: ""|paused|takeover|stopped|moved} hide show quit
hide = task finished / agent idle: remove border, cursor and panel until the agent acts again.
Writes user commands to stdout: PANIC PAUSE RESUME TAKEOVER GIVEBACK STOP "MODE strict|normal".
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import sys
import threading
import time
from ctypes import wintypes

from . import theme as T

KEY = "#010203"  # transparent colour key
AGENT = "Claude"


def emit(cmd: str):
    sys.stdout.write(cmd + "\n")
    sys.stdout.flush()


def _hotkey_thread(q: queue.Queue):
    user32 = ctypes.windll.user32
    if not user32.RegisterHotKey(None, 1, 0x2 | 0x1 | 0x4000, 0x1B):  # Ctrl+Alt+Esc, no repeat
        return
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == 0x0312:
            emit("PANIC")
            q.put({"op": "toggle_pause"})


def _stdin_thread(q: queue.Queue):
    for line in sys.stdin:
        try:
            q.put(json.loads(line))
        except Exception:
            pass
    q.put({"op": "quit"})


def _style(hwnd: int, clickthrough: bool):
    user32 = ctypes.windll.user32
    ex = user32.GetWindowLongW(hwnd, -20)
    ex |= 0x00000080 | 0x08000000 | 0x00000008          # toolwindow | noactivate | topmost
    if clickthrough:
        ex |= 0x00080000 | 0x00000020                    # layered | transparent
    user32.SetWindowLongW(hwnd, -20, ex)
    if not os.environ.get("DESKPILOT_SHOW_UI"):
        user32.SetWindowDisplayAffinity(hwnd, 0x11)      # hidden from screenshots / OCR
    v = ctypes.c_int(2)                                  # Win11 rounded corners
    ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(v), ctypes.sizeof(v))


def _hwnd(win) -> int:
    win.update_idletasks()
    return ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()


def _cursor_pos() -> tuple[int, int]:
    p = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def _round_rect(c, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2, x1, y2,
           x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


STATES = {  # state -> (label, colour, active?)  active = filled dot + solid border; else hollow + dashed
    "idle": ("Idle", T.FAINT, False),
    "working": ("Working", T.FG, True),
    "asking": ("Needs your OK", T.FG, True),
    "paused": ("Paused", T.MUTED, False),
    "takeover": ("You're in control", T.MUTED, False),
    "stopped": ("Stopped", T.MUTED, False),
    "moved": ("Paused · mouse moved", T.MUTED, False),
}


class Panel:
    """Floating control panel: status, current step, activity log, stats, Pause / Take over / Stop."""

    def __init__(self, app: "Overlay"):
        import customtkinter as ctk

        self.app = app
        self.ctk = ctk
        self.win = ctk.CTkToplevel(app.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(fg_color=T.BORDER)
        self.collapsed = False
        self.hidden_until = 0.0

        body = ctk.CTkFrame(self.win, fg_color=T.BG, corner_radius=16)
        body.pack(fill="both", expand=True, padx=1, pady=1)
        self.body = body

        head = ctk.CTkFrame(body, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(14, 0))
        self.dot = ctk.CTkLabel(head, text="○", text_color=T.FAINT, font=T.font(13), width=14)
        self.dot.pack(side="left")
        name = ctk.CTkLabel(head, text=f" {AGENT}", text_color=T.FG, font=T.font(14, "bold"))
        name.pack(side="left")
        self.min_btn = ctk.CTkButton(head, text="–", width=26, height=24, corner_radius=12, fg_color="transparent",
                                     hover_color=T.CARD_HOVER, text_color=T.MUTED, font=T.font(13),
                                     command=self.toggle_collapse)
        self.min_btn.pack(side="right")
        self.state_lbl = ctk.CTkLabel(head, text="Idle", text_color=T.FAINT, font=T.font(11), height=22,
                                      corner_radius=11, fg_color=T.CARD, padx=10)
        self.state_lbl.pack(side="right", padx=(0, 6))
        for w in (head, self.state_lbl, self.dot, name):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag)

        self.more = ctk.CTkFrame(body, fg_color="transparent")
        self.more.pack(fill="both", expand=True)
        T.divider(ctk, self.more, pady=(12, 10), padx=16)
        ctk.CTkLabel(self.more, text="NOW", text_color=T.FAINT, font=T.font(9, "bold"), anchor="w").pack(
            fill="x", padx=16)
        self.now = ctk.CTkLabel(self.more, text="Ready", text_color=T.FG, font=T.font(13, "bold"), anchor="w",
                                justify="left", wraplength=300)
        self.now.pack(fill="x", padx=16, pady=(0, 8))
        logbox = ctk.CTkFrame(self.more, fg_color=T.CARD, corner_radius=10)
        logbox.pack(fill="x", padx=16)
        self.log = ctk.CTkLabel(logbox, text="No activity yet", text_color=T.MUTED, font=T.font(10, family=T.MONO),
                                anchor="nw", justify="left", wraplength=290, height=92)
        self.log.pack(fill="x", padx=10, pady=8)
        self.stats = ctk.CTkLabel(self.more, text="0 actions · ~0 tokens", text_color=T.FAINT, font=T.font(10),
                                  anchor="w")
        self.stats.pack(fill="x", padx=16, pady=(8, 10))

        row = ctk.CTkFrame(self.more, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 4))
        row.grid_columnconfigure((0, 1, 2), weight=1, uniform="b")
        self.pause_btn = T.button(ctk, row, "Pause", self.on_pause, kind="secondary", width=90)
        self.pause_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.take_btn = T.button(ctk, row, "Take over", self.on_take, kind="secondary", width=90)
        self.take_btn.grid(row=0, column=1, sticky="ew", padx=4)
        self.stop_btn = T.button(ctk, row, "Stop", self.on_stop, kind="primary", width=90)
        self.stop_btn.grid(row=0, column=2, sticky="ew", padx=(4, 0))

        T.divider(ctk, self.more, pady=(12, 8), padx=16)
        self.strict = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(self.more, text="Ask before every action", variable=self.strict, command=self.on_mode,
                      font=T.font(11), text_color=T.MUTED, fg_color=T.CARD_HOVER, progress_color=T.FG,
                      button_color=T.MUTED, button_hover_color=T.FG, switch_width=34, switch_height=18).pack(
            anchor="w", padx=16, pady=(0, 6))
        ctk.CTkLabel(self.more, text="Ctrl + Alt + Esc  pauses or resumes anytime", text_color=T.FAINT,
                     font=T.font(9), anchor="w").pack(fill="x", padx=16, pady=(0, 12))

        self.lines: list[str] = []
        self.win.update_idletasks()
        _style(_hwnd(self.win), clickthrough=False)

    # -- placement
    def place(self, panel_area):
        l, t, w, h = panel_area
        self.win.update()
        pw, ph = max(self.win.winfo_reqwidth(), 340), self.win.winfo_reqheight()
        # raw Tk call: CTk's geometry() would rescale the (already physical) pixel values
        self.win.tk.call("wm", "geometry", self.win._w, f"{pw}x{ph}+{l + w - pw - 24}+{t + h - ph - 72}")

    def bbox(self):
        x, y = self.win.winfo_rootx(), self.win.winfo_rooty()
        return x, y, x + self.win.winfo_width(), y + self.win.winfo_height()

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.win.winfo_rootx(), e.y_root - self.win.winfo_rooty()

    def _drag(self, e):
        self.win.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def toggle_collapse(self):
        self.collapsed = not self.collapsed
        if self.collapsed:
            self.more.pack_forget()
            self.min_btn.configure(text="+")
        else:
            self.more.pack(fill="both", expand=True)
            self.min_btn.configure(text="–")

    # -- buttons
    def on_pause(self):
        emit("RESUME" if self.app.paused else "PAUSE")

    def on_take(self):
        emit("GIVEBACK" if self.app.paused == "takeover" else "TAKEOVER")

    def on_stop(self):
        emit("RESUME" if self.app.paused == "stopped" else "STOP")

    def on_mode(self):
        emit("MODE " + ("strict" if self.strict.get() else "normal"))

    # -- updates
    def add_log(self, text: str):
        self.now.configure(text=text[:90])
        self.lines.append(time.strftime("%H:%M:%S ") + text[:46])
        self.lines = self.lines[-6:]
        self.log.configure(text="\n".join(reversed(self.lines)))

    def refresh(self):
        a = self.app
        key = a.paused or a.state
        label, col, active = STATES.get(key, STATES["idle"])
        self.dot.configure(text="●" if active else "○", text_color=col)
        self.state_lbl.configure(text=label, text_color=T.ON_ACCENT if active else col,
                                 fg_color=T.FG if active else T.CARD)
        self.pause_btn.configure(text="Resume" if a.paused in ("paused", "moved") else "Pause")
        self.take_btn.configure(text="Give back" if a.paused == "takeover" else "Take over")
        self.stop_btn.configure(text="Unlock" if a.paused == "stopped" else "Stop")
        self.stats.configure(text=f"{a.actions} actions · ~{_k(a.tokens)} tokens sent to the AI")


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


class Overlay:
    def __init__(self):
        import customtkinter as ctk
        import tkinter as tk

        ctk.set_appearance_mode("dark")
        self.tk = tk
        self.root = ctk.CTk()
        self.root.withdraw()
        self.q: queue.Queue = queue.Queue()
        self.screens: list[dict] = []
        self.paused = ""
        self.state = "idle"
        self.state_text = ""
        self.tokens = self.actions = 0
        self.cursor = None
        self.last_activity = time.time()
        self.label_text = ""
        self.label_until = 0.0
        self.ripples: list[tuple[int, int, float]] = []
        self.panel = Panel(self)
        self.panel_area = None
        self.hidden = False
        threading.Thread(target=_stdin_thread, args=(self.q,), daemon=True).start()
        threading.Thread(target=_hotkey_thread, args=(self.q,), daemon=True).start()
        self.root.after(15, self.tick)

    def set_areas(self, rects, mode):
        from .config import monitors

        for s in self.screens:
            s["win"].destroy()
        self.screens = []
        for (l, t, w, h) in rects:
            win = self.tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.geometry(f"{w}x{h}+{l}+{t}")
            win.configure(bg=KEY)
            win.attributes("-transparentcolor", KEY)
            win.attributes("-topmost", True)
            c = self.tk.Canvas(win, width=w, height=h, bg=KEY, highlightthickness=0)
            c.pack()
            _style(_hwnd(win), clickthrough=True)
            self.screens.append({"win": win, "c": c, "rect": (l, t, w, h)})
        # Panel goes on a screen the agent does NOT control when possible (so it never covers its work).
        controlled = {tuple(r) for r in rects}
        others = [(m.left, m.top, m.width, m.height) for m in monitors()
                  if (m.left, m.top, m.width, m.height) not in controlled]
        self.panel_area = others[0] if others else tuple(rects[0])
        self.panel_shares_screen = not others
        self.panel.strict.set(mode == "strict")
        self.panel.place(self.panel_area)
        self.root.after(400, lambda: (self.panel.place(self.panel_area), self.panel.win.lift()))

    def hide(self):
        if self.hidden:
            return
        self.hidden = True
        for s in self.screens:
            s["win"].withdraw()
        self.panel.win.withdraw()
        self.cursor = None
        self.ripples = []
        self.label_text = ""

    def show(self):
        if not self.hidden:
            return
        self.hidden = False
        for s in self.screens:
            s["win"].deiconify()
            s["win"].attributes("-topmost", True)
        self.panel.win.deiconify()
        if self.panel_area:
            self.panel.place(self.panel_area)
        self.panel.win.lift()

    def handle(self, m: dict):
        op = m.get("op")
        if op == "hide":
            self.hide()
            return
        # anything the agent (or the Ctrl+Alt+Esc hotkey) does brings the overlay back
        if op in ("show", "areas", "move", "click", "paused", "toggle_pause") or (
                op == "state" and m.get("v") in ("working", "asking")):
            self.show()
        if op == "areas":
            self.set_areas(m["rects"], m.get("mode", "normal"))
        elif op == "move":
            self.cursor = (m["x"], m["y"])
            self.last_activity = time.time()
        elif op == "click":
            self.cursor = (m["x"], m["y"])
            self.ripples.append((m["x"], m["y"], time.time()))
            self.last_activity = time.time()
        elif op == "label":
            self.label_text = m.get("text", "")
            self.label_until = time.time() + 1.6
        elif op == "state":
            self.state, self.state_text = m.get("v", "idle"), m.get("text", "")
            if self.state == "asking":
                self.panel.add_log("? " + self.state_text)
        elif op == "log":
            self.panel.add_log(m.get("text", ""))
        elif op == "stats":
            self.tokens, self.actions = m.get("tokens", 0), m.get("actions", 0)
        elif op == "paused":
            self.paused = m.get("v") or ""
        elif op == "toggle_pause":
            pass  # the server answers with a "paused" message
        elif op == "quit":
            self.root.destroy()
            raise SystemExit
        self.panel.refresh()

    def tick(self):
        try:
            while True:
                self.handle(self.q.get_nowait())
        except queue.Empty:
            pass
        except SystemExit:
            return
        now = time.time()
        self.ripples = [r for r in self.ripples if now - r[2] < 0.5]
        if not self.hidden:
            self._auto_hide_panel(now)
            for s in self.screens:
                self.draw(s, now)
        self.root.after(16, self.tick)

    def _auto_hide_panel(self, now):
        if not getattr(self, "panel_shares_screen", False) or not self.cursor:
            return
        x1, y1, x2, y2 = self.panel.bbox()
        x, y = self.cursor
        near = x1 - 40 <= x <= x2 + 40 and y1 - 40 <= y <= y2 + 40
        if near and now - self.last_activity < 1:
            if self.panel.win.state() != "withdrawn":
                self.panel.win.withdraw()
            self.panel.hidden_until = now + 2.5
        elif self.panel.win.state() == "withdrawn" and now > self.panel.hidden_until:
            self.panel.win.deiconify()

    def draw(self, s, now):
        c = s["c"]
        l, t, w, h = s["rect"]
        c.delete("all")
        key = self.paused or self.state
        _, _, active = STATES.get(key, STATES["working"])
        active = active or key == "idle"
        dash = () if active else (10, 6)
        # border: black outer line + white inner line, so it reads on both light and dark apps
        c.create_rectangle(2, 2, w - 3, h - 3, outline=T.INK, width=4)
        c.create_rectangle(2, 2, w - 3, h - 3, outline=T.FG, width=2, dash=dash)
        # status chip: black pill, white hairline, dot + text
        if self.paused:
            text = {"takeover": "You're in control  ·  Give back from the panel",
                    "stopped": "Agent stopped  ·  Unlock from the panel"}.get(
                self.paused, "Agent paused  ·  Ctrl+Alt+Esc to resume")
        elif self.state == "asking":
            text = "Waiting for your permission"
        else:
            text = f"{AGENT} is using this screen  ·  Ctrl+Alt+Esc to pause"
        tw = int(6.9 * len(text)) + 56
        x1, x2 = w // 2 - tw // 2, w // 2 + tw // 2
        _round_rect(c, x1, 8, x2, 38, 15, fill=T.INK, outline=T.FG, width=1)
        if active and int(now * 1.6) % 2 == 0 and key in ("working", "asking"):
            c.create_oval(x1 + 16, 19, x1 + 24, 27, fill=T.FG, outline=T.FG)
        elif active:
            c.create_oval(x1 + 16, 19, x1 + 24, 27, fill=T.MUTED, outline=T.MUTED)
        else:
            c.create_rectangle(x1 + 16, 19, x1 + 18, 27, fill=T.FG, outline=T.FG)  # "pause" bars
            c.create_rectangle(x1 + 21, 19, x1 + 23, 27, fill=T.FG, outline=T.FG)
        c.create_text(x1 + 34, 23, text=text, anchor="w", fill=T.FG, font=("Segoe UI Semibold", 10))
        # ripples: white ring with a black edge
        for (x, y, t0) in self.ripples:
            if l <= x < l + w and t <= y < t + h:
                k = (now - t0) / 0.5
                r = 8 + 28 * k
                wd = max(1, int(4 * (1 - k)))
                cx, cy = x - l, y - t
                c.create_oval(cx - r - wd, cy - r - wd, cx + r + wd, cy + r + wd, outline=T.INK, width=1)
                c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=T.FG, width=wd)
        # No fake cursor: the agent drives the real Windows cursor. While it is acting, a small name tag
        # rides next to the real pointer (read live, so it never lags behind a second arrow).
        labelled = self.label_text and now < self.label_until
        if not self.paused and (labelled or now - self.last_activity < 1.5):
            x, y = _cursor_pos()
            if l <= x < l + w and t <= y < t + h:
                x, y = x - l, y - t
                tag = f"{AGENT} · {self.label_text}" if labelled else AGENT
                pw = int(6.8 * len(tag)) + 24
                _round_rect(c, x + 20, y + 24, x + 20 + pw, y + 46, 11, fill=T.INK, outline=T.FG, width=1)
                c.create_text(x + 32, y + 35, text=tag, anchor="w", fill=T.FG, font=("Segoe UI Semibold", 9))

    def run(self):
        self.root.mainloop()


def main():
    from .config import set_dpi_awareness

    set_dpi_awareness()
    Overlay().run()


if __name__ == "__main__":
    main()
