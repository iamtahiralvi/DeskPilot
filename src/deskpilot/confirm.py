"""Ask-first permission dialog. `python -m deskpilot.confirm <title> <detail> [timeout]` -> prints once|session|deny."""
from __future__ import annotations

import sys

from . import theme as T


def ask(title: str, detail: str, timeout: int = 60) -> str:
    import customtkinter as ctk

    answer = "deny"
    root = T.window(ctk, "DeskPilot - permission")

    def done(v: str):
        nonlocal answer
        answer = v
        root.destroy()

    pad = ctk.CTkFrame(root, fg_color="transparent")
    pad.pack(padx=28, pady=24)
    head = ctk.CTkFrame(pad, fg_color="transparent")
    head.pack(fill="x")
    ctk.CTkLabel(head, text="!", width=36, height=36, corner_radius=18, fg_color=T.FG, text_color=T.ON_ACCENT,
                 font=T.font(18, "bold")).pack(side="left", anchor="n")
    box = ctk.CTkFrame(head, fg_color="transparent")
    box.pack(side="left", padx=(14, 0))
    ctk.CTkLabel(box, text="CLAUDE WANTS TO", text_color=T.FAINT, font=T.font(9, "bold"), anchor="w").pack(fill="x")
    ctk.CTkLabel(box, text=title, text_color=T.FG, font=T.font(17, "bold"), anchor="w", justify="left",
                 wraplength=440).pack(fill="x")

    if detail:
        d = ctk.CTkTextbox(pad, width=500, height=min(170, 34 + 20 * (detail.count("\n") + 1)), fg_color=T.CARD,
                           text_color=T.MUTED, font=T.font(11, family=T.MONO), corner_radius=10, border_width=1,
                           border_color=T.BORDER)
        d.insert("1.0", detail)
        d.configure(state="disabled")
        d.pack(fill="x", pady=(16, 0))

    bar = ctk.CTkProgressBar(pad, width=500, height=2, progress_color=T.FG, fg_color=T.BORDER, corner_radius=1)
    bar.set(1)
    bar.pack(fill="x", pady=(18, 6))
    left = ctk.CTkLabel(pad, text="", text_color=T.FAINT, font=T.font(10), anchor="w")
    left.pack(fill="x")

    row = ctk.CTkFrame(pad, fg_color="transparent")
    row.pack(fill="x", pady=(16, 0))
    T.button(ctk, row, "Deny", lambda: done("deny"), kind="danger", width=96).pack(side="left")
    T.button(ctk, row, "Allow once", lambda: done("once"), width=120).pack(side="right")
    T.button(ctk, row, "Allow for session", lambda: done("session"), kind="secondary", width=150).pack(
        side="right", padx=8)
    root.bind("<Escape>", lambda _e: done("deny"))
    root.protocol("WM_DELETE_WINDOW", lambda: done("deny"))

    remaining = [timeout * 10]

    def tick():
        remaining[0] -= 1
        if remaining[0] <= 0:
            done("deny")
            return
        bar.set(remaining[0] / (timeout * 10))
        left.configure(text=f"Denied automatically in {remaining[0] // 10 + 1}s  ·  Esc = deny")
        root.after(100, tick)

    tick()
    T.center(root)
    root.bell()
    root.mainloop()
    return answer


if __name__ == "__main__":
    t = sys.argv[1] if len(sys.argv) > 1 else "Perform an action"
    d = sys.argv[2] if len(sys.argv) > 2 else ""
    to = int(sys.argv[3]) if len(sys.argv) > 3 else 60
    sys.stdout.write(ask(t, d, to))
