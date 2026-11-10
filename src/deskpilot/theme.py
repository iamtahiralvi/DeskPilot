"""Shared look for every DeskPilot window (picker, permission dialog, control panel, overlay).

Monochrome: near-black surfaces, white type, grey for secondary text. State is shown by shape and words
(filled vs hollow dot, solid vs dashed border), not by colour.
"""
from __future__ import annotations

BG = "#0a0a0a"
CARD = "#161616"
CARD_HOVER = "#232323"
BORDER = "#2b2b2b"
FG = "#fafafa"
MUTED = "#a3a3a3"
FAINT = "#5e5e5e"
ACCENT = "#fafafa"          # primary = white
ACCENT_HOVER = "#d6d6d6"
ACCENT_DIM = "#9a9a9a"
ON_ACCENT = "#0a0a0a"       # text on white
INK = "#0a0a0a"             # overlay "black" (not the transparent key)
SELECTED = "#4a4a4a"       # selected segment / toggle state
# Kept for compatibility with older callers; everything is monochrome now.
DANGER = FG
DANGER_HOVER = ACCENT_HOVER
WARN = FG
INFO = MUTED
OK = FG
FONT = "Segoe UI"
MONO = "Cascadia Mono"


def font(size: int, weight: str = "normal", family: str = FONT):
    import customtkinter as ctk

    return ctk.CTkFont(family=family, size=size, weight=weight)


def button(ctk, master, text, command, kind="primary", width=120, height=34):
    styles = {
        "primary": dict(fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=ON_ACCENT),
        "secondary": dict(fg_color=CARD, hover_color=CARD_HOVER, text_color=FG, border_width=1, border_color=BORDER),
        "danger": dict(fg_color="transparent", hover_color=CARD_HOVER, text_color=FG, border_width=1,
                       border_color=FG),
        "ghost": dict(fg_color="transparent", hover_color=CARD_HOVER, text_color=MUTED),
    }
    return ctk.CTkButton(master, text=text, command=command, width=width, height=height,
                         corner_radius=height // 2, font=font(12, "bold"), **styles[kind])


def divider(ctk, master, pady=(10, 10), padx=0):
    line = ctk.CTkFrame(master, height=1, fg_color=BORDER, corner_radius=0)
    line.pack(fill="x", padx=padx, pady=pady)
    return line


def window(ctk, title: str):
    from .config import set_dpi_awareness

    set_dpi_awareness()
    ctk.set_appearance_mode("dark")
    root = ctk.CTk()
    root.title(title)
    root.configure(fg_color=BG)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    return root


def center(root, monitor=None):
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    if monitor is None:
        x, y = (root.winfo_screenwidth() - w) // 2, (root.winfo_screenheight() - h) // 3
    else:
        x, y = monitor.left + (monitor.width - w) // 2, monitor.top + (monitor.height - h) // 3
    root.geometry(f"+{max(0, x)}+{max(0, y)}")
    root.lift()
    root.focus_force()
