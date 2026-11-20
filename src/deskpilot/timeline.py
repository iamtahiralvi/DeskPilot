"""Step log with thumbnails: what the agent did, step by step, so the user can review it (also after a kill).

Each step appends a line to ~/.deskpilot/timeline/timeline.jsonl and, when there is one, saves a small JPEG of
the screen right after it (private windows are blacked out first). `open_html()` renders the recent log as a
local page. Files older than keep_days are deleted.
"""
from __future__ import annotations

import html
import json
import os
import threading
import time

from .config import HOME

DIR = HOME / "timeline"
LOG = DIR / "timeline.jsonl"
PAGE = DIR / "timeline.html"
THUMB = (480, 300)
_lock = threading.Lock()


def append(entry: dict) -> None:
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        with _lock, LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), **entry}, ensure_ascii=False) + "\n")
    except OSError:
        pass


class Timeline:
    def __init__(self, enabled: bool = True, keep_days: float = 7, session: str = ""):
        self.enabled = enabled
        self.keep_days = keep_days
        self.session = session
        self.n = 0
        if enabled:
            threading.Thread(target=prune, args=(keep_days,), daemon=True).start()

    def record(self, text: str, ok: bool = True, img=None, kind: str = "step") -> None:
        """img: a PIL image of the screen just after the step (already privacy-masked), or None."""
        if not self.enabled:
            return
        self.n += 1
        entry = {"pid": os.getpid(), "session": self.session, "kind": kind, "text": text[:200], "ok": ok}
        if img is None:
            append(entry)
            return
        name = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}-{self.n}.jpg"
        entry["thumb"] = name
        append(entry)  # the step is logged now, even if the process ends before the picture is saved

        def save():  # encoding off the worker thread keeps steps fast
            try:
                im = img.copy()
                im.thumbnail(THUMB)
                DIR.mkdir(parents=True, exist_ok=True)
                im.save(DIR / name, "JPEG", quality=60)
            except Exception:
                pass

        threading.Thread(target=save, daemon=True).start()


def prune(keep_days: float = 7) -> None:
    if not DIR.exists():
        return
    cutoff = time.time() - keep_days * 86400
    try:
        for p in DIR.glob("*.jpg"):
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
        if LOG.exists():
            keep = [ln for ln in LOG.read_text(encoding="utf-8").splitlines() if _t(ln) >= cutoff]
            with _lock:
                LOG.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    except OSError:
        pass


def _t(line: str) -> float:
    try:
        return float(json.loads(line).get("t", 0))
    except (ValueError, AttributeError):
        return 0.0


def entries(limit: int = 400) -> list[dict]:
    try:
        lines = LOG.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


_CSS = """
:root{--bg:#0a0a0a;--card:#161616;--line:#2b2b2b;--fg:#fafafa;--muted:#a3a3a3;--faint:#5e5e5e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 "Segoe UI",system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding:28px 16px 60px}h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 24px}h2{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);
margin:28px 0 10px;border-top:1px solid var(--line);padding-top:16px}
.row{display:grid;grid-template-columns:72px 1fr auto;gap:14px;align-items:start;padding:10px 12px;border-radius:10px}
.row:nth-child(odd){background:var(--card)}.time{color:var(--faint);font:12px "Cascadia Mono",Consolas,monospace;padding-top:2px}
.text{word-break:break-word}.bad .text{color:#ff9b9b}.kill .text,.mark .text{font-weight:600}
img{width:240px;max-width:40vw;border:1px solid var(--line);border-radius:6px;display:block;cursor:zoom-in}
img.big{width:min(960px,92vw);max-width:none;cursor:zoom-out}
@media (max-width:640px){.row{grid-template-columns:56px 1fr}.row img{grid-column:1/-1;width:100%;max-width:100%}}
"""


def build_html(limit: int = 400) -> str:
    rows = entries(limit)
    rows.reverse()  # newest first
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,"
             f"initial-scale=1'><title>DeskPilot timeline</title><style>{_CSS}</style></head><body><main>",
             "<h1>What the agent did</h1>",
             f"<p class='sub'>Newest first · {len(rows)} steps · generated {time.strftime('%H:%M:%S')} · "
             "private windows are blacked out in the pictures</p>"]
    day = None
    for e in rows:
        t = time.localtime(e.get("t", 0))
        d = time.strftime("%A %d %B", t)
        if d != day:
            parts.append(f"<h2>{html.escape(d)}</h2>")
            day = d
        cls = "row" + ("" if e.get("ok", True) else " bad") + (f" {e.get('kind')}" if e.get("kind") != "step" else "")
        who = html.escape(e.get("session") or "")
        img = (f"<img src='{html.escape(e['thumb'])}' alt='screen after this step' loading='lazy' "
               "onerror=\"this.remove()\" onclick=\"this.classList.toggle('big')\">") if e.get("thumb") else "<span></span>"
        parts.append(f"<div class='{cls}'><div class='time'>{time.strftime('%H:%M:%S', t)}</div>"
                     f"<div class='text'>{html.escape(e.get('text', ''))}"
                     f"{f'<div class=time>{who}</div>' if who else ''}</div>{img}</div>")
    if not rows:
        parts.append("<p class='sub'>Nothing recorded yet.</p>")
    parts.append("</main></body></html>")
    DIR.mkdir(parents=True, exist_ok=True)
    PAGE.write_text("".join(parts), encoding="utf-8")
    return str(PAGE)


def open_html() -> str:
    path = build_html()
    try:
        os.startfile(path)  # type: ignore[attr-defined]  (Windows)
    except (AttributeError, OSError):
        pass
    return path
