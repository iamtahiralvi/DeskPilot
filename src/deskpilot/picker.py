"""Screen picker. Run as `python -m deskpilot.picker`; prints the choice as JSON on stdout."""
from __future__ import annotations

import json
import sys

from . import theme as T
from .config import Session, monitors


def run_picker() -> dict | None:
    import customtkinter as ctk
    import mss
    from PIL import Image

    root = T.window(ctk, "DeskPilot")
    prev = Session.load()
    mons = monitors()
    result: dict | None = None
    choice = {"v": str(prev.monitor) if str(prev.monitor) in [str(i) for i in range(len(mons))] + ["all"] else "0"}

    pad = ctk.CTkFrame(root, fg_color="transparent")
    pad.pack(padx=28, pady=24)

    head = ctk.CTkFrame(pad, fg_color="transparent")
    head.pack(fill="x")
    ctk.CTkLabel(head, text="●", text_color=T.FG, font=T.font(18)).pack(side="left")
    ctk.CTkLabel(head, text="  DeskPilot", text_color=T.FG, font=T.font(20, "bold")).pack(side="left")
    ctk.CTkLabel(pad, text="Choose the screen your AI agent can see and control", text_color=T.FG,
                 font=T.font(15, "bold"), anchor="w").pack(fill="x", pady=(16, 2))
    ctk.CTkLabel(pad, text="A thin border marks that screen while the agent works. Pause it any time "
                 "with Ctrl+Alt+Esc or from the control panel.", text_color=T.MUTED, font=T.font(11),
                 anchor="w", justify="left", wraplength=640).pack(fill="x", pady=(0, 18))

    grid = ctk.CTkFrame(pad, fg_color="transparent")
    grid.pack(fill="x")
    cards: dict[str, ctk.CTkFrame] = {}
    keep = []

    def select(k):
        choice["v"] = k
        for key, f in cards.items():
            on = key == k
            f.configure(border_color=T.ACCENT if on else T.BORDER, fg_color=T.CARD_HOVER if on else T.CARD)

    def card(key, title, sub, img=None):
        f = ctk.CTkFrame(grid, fg_color=T.CARD, corner_radius=12, border_width=2, border_color=T.BORDER,
                         cursor="hand2")
        f.pack(side="left", padx=(0, 12), anchor="n")
        widgets = [f]
        if img is not None:
            ci = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
            keep.append(ci)
            lbl = ctk.CTkLabel(f, image=ci, text="")
            lbl.pack(padx=10, pady=(10, 6))
            widgets.append(lbl)
        else:
            sp = ctk.CTkLabel(f, text="⧉", text_color=T.MUTED, font=T.font(48), width=160, height=img_h)
            sp.pack(padx=10, pady=(10, 6))
            widgets.append(sp)
        t1 = ctk.CTkLabel(f, text=title, text_color=T.FG, font=T.font(13, "bold"), anchor="w")
        t1.pack(fill="x", padx=12)
        t2 = ctk.CTkLabel(f, text=sub, text_color=T.MUTED, font=T.font(11), anchor="w")
        t2.pack(fill="x", padx=12, pady=(0, 10))
        widgets += [t1, t2]
        for w in widgets:
            w.bind("<Button-1>", lambda _e, k=key: select(k))
        cards[key] = f

    img_h = 150
    with mss.MSS() as s:
        for i, m in enumerate(mons):
            shot = s.grab({"left": m.left, "top": m.top, "width": m.width, "height": m.height})
            img = Image.frombytes("RGB", shot.size, shot.rgb)
            img.thumbnail((260, img_h))
            main = "  ·  main" if m.left == 0 and m.top == 0 else ""
            card(str(i), f"Screen {i + 1}{main}", f"{m.width} × {m.height}", img)
    if len(mons) > 1:
        card("all", "All screens", "agent can use every monitor")
    select(choice["v"])

    ctk.CTkLabel(pad, text="Permissions", text_color=T.FG, font=T.font(13, "bold"), anchor="w").pack(
        fill="x", pady=(20, 6))
    mode = ctk.StringVar(value="Normal" if prev.mode != "strict" else "Strict")
    desc = ctk.CTkLabel(pad, text="", text_color=T.MUTED, font=T.font(11), anchor="w")

    def on_mode(v=None):
        desc.configure(text="Acts freely; asks you before deleting, paying, sending, installing, closing…"
                       if mode.get() == "Normal" else "Asks you before every batch of actions.")

    ctk.CTkSegmentedButton(pad, values=["Normal", "Strict"], variable=mode, command=on_mode,
                           selected_color=T.SELECTED, selected_hover_color=T.SELECTED,
                           unselected_color=T.CARD, unselected_hover_color=T.CARD_HOVER,
                           text_color=T.FG, font=T.font(12, "bold"), width=220, height=32).pack(anchor="w")
    desc.pack(fill="x", pady=(6, 0))
    on_mode()

    foot = ctk.CTkFrame(pad, fg_color="transparent")
    foot.pack(fill="x", pady=(22, 0))
    remember = ctk.BooleanVar(value=prev.remember)
    ctk.CTkCheckBox(foot, text="Remember my choice", variable=remember, font=T.font(11), text_color=T.MUTED,
                    fg_color=T.ACCENT, hover_color=T.ACCENT_HOVER, border_color=T.FAINT,
                    checkmark_color=T.ON_ACCENT, corner_radius=4).pack(side="left")

    def ok(_e=None):
        nonlocal result
        v = choice["v"]
        result = {"monitor": "all" if v == "all" else int(v), "mode": mode.get().lower(), "remember": remember.get()}
        root.destroy()

    T.button(ctk, foot, "Start agent", ok, width=140).pack(side="right")
    T.button(ctk, foot, "Cancel", root.destroy, kind="ghost", width=90).pack(side="right", padx=8)
    root.bind("<Return>", ok)
    root.bind("<Escape>", lambda _e: root.destroy())
    primary = next((m for m in mons if m.left == 0 and m.top == 0), mons[0])
    T.center(root, primary)
    root.after(180_000, root.destroy)
    root.mainloop()
    return result


if __name__ == "__main__":
    res = run_picker()
    sys.stdout.write(json.dumps(res) if res else "null")
