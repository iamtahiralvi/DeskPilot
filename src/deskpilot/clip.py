"""Clipboard access that leaves the user's clipboard as it was.

`paste()` saves what is on the clipboard, puts the text there (hidden from Windows clipboard history and cloud
sync), presses Ctrl+V and puts the user's content back. Text, rich text, HTML and images (DIB) survive the round
trip; formats that only exist as handles (file lists from Explorer, metafiles) can't be copied and are reported.
"""
from __future__ import annotations

import time

import win32clipboard as cb

# Handle-only or synthesized formats: Windows makes these from the others, or they can't be read as bytes.
_SKIP = {1, 2, 3, 7, 9, 14, 15, 16, 17, 0x80, 0x82, 0x83, 0x8E}
_NAMED_SKIP = {"ExcludeClipboardContentFromMonitorProcessing", "CanIncludeInClipboardHistory",
               "CanUploadToCloudClipboard"}
MAX_FILE = 2_000_000


class _Open:
    def __enter__(self):
        for _ in range(20):  # another app may hold it for a moment
            try:
                cb.OpenClipboard()
                return self
            except Exception:
                time.sleep(0.05)
        raise RuntimeError("the clipboard is busy (another app has it open)")

    def __exit__(self, *exc):
        try:
            cb.CloseClipboard()
        except Exception:
            pass


def get_text() -> str | None:
    with _Open():
        try:
            return cb.GetClipboardData(cb.CF_UNICODETEXT)
        except Exception:
            return None


def set_text(text: str, private: bool = False) -> None:
    """private=True keeps it out of Win+V history and cloud clipboard (used for temporary pastes)."""
    with _Open():
        cb.EmptyClipboard()
        cb.SetClipboardData(cb.CF_UNICODETEXT, text)
        if private:
            for name, val in (("ExcludeClipboardContentFromMonitorProcessing", b"\0\0\0\0"),
                              ("CanIncludeInClipboardHistory", b"\0\0\0\0"),
                              ("CanUploadToCloudClipboard", b"\0\0\0\0")):
                try:
                    cb.SetClipboardData(cb.RegisterClipboardFormat(name), val)
                except Exception:
                    pass


def save() -> tuple[list[tuple[int, object]], int]:
    """Copy of the clipboard: ([(format, data)], number of formats that could not be saved)."""
    saved, lost = [], 0
    with _Open():
        fmt = cb.EnumClipboardFormats(0)
        while fmt:
            name = ""
            if fmt >= 0xC000:
                try:
                    name = cb.GetClipboardFormatName(fmt)
                except Exception:
                    name = ""
            if fmt not in _SKIP and name not in _NAMED_SKIP:
                try:
                    data = cb.GetClipboardData(fmt)
                    if isinstance(data, (bytes, str)):
                        saved.append((fmt, data))
                    else:
                        lost += 1
                except Exception:
                    lost += 1
            elif fmt == 15:  # a list of files copied in Explorer
                lost += 1
            fmt = cb.EnumClipboardFormats(fmt)
    return saved, lost


def restore(saved: list[tuple[int, object]]) -> None:
    with _Open():
        cb.EmptyClipboard()
        for fmt, data in saved:
            try:
                cb.SetClipboardData(fmt, data)
            except Exception:
                pass


def read_file(path: str) -> str:
    import os

    p = os.path.expandvars(os.path.expanduser(path))
    if not os.path.isfile(p):
        raise FileNotFoundError(f"no such file: {path}")
    if os.path.getsize(p) > MAX_FILE:
        raise ValueError(f"{path} is over {MAX_FILE // 1_000_000} MB; too big to paste")
    raw = open(p, "rb").read()
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            text = raw.decode(enc)
            if enc != "utf-16" or raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
                return text
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")
