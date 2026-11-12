"""Precise web-page reading through the Chrome DevTools Protocol (Chrome, Edge, Brave).

Clicks/typing still go through the real mouse + keyboard (visible cursor); CDP is only used to read the
page and to locate/scroll elements. Connects to:
  * the DeskPilot browser profile (~/.deskpilot/browser-profile, started by launch("agent browser"))
  * your normal Chrome/Edge/Brave if remote debugging is enabled at chrome://inspect/#remote-debugging
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .config import HOME

BROWSER_EXES = {"chrome.exe", "msedge.exe", "brave.exe", "chromium.exe"}
PROFILE_DIR = HOME / "browser-profile"
_LA = os.environ.get("LOCALAPPDATA", "")
USER_DATA_DIRS = [
    PROFILE_DIR,
    Path(_LA) / "Google/Chrome/User Data",
    Path(_LA) / "Microsoft/Edge/User Data",
    Path(_LA) / "BraveSoftware/Brave-Browser/User Data",
    Path(_LA) / "Chromium/User Data",
]


class CDP:
    def __init__(self, ws_url: str):
        import websocket

        self.url = ws_url
        self.ws = websocket.create_connection(ws_url, timeout=8, suppress_origin=True, enable_multithread=False)
        self.n = 0
        self.sessions: dict[str, str] = {}  # targetId -> sessionId

    def call(self, method: str, params: dict | None = None, session: str | None = None, timeout: float = 10):
        self.n += 1
        mid = self.n
        msg = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.send(json.dumps(msg))
        end = time.time() + timeout
        while time.time() < end:
            self.ws.settimeout(max(0.1, end - time.time()))
            data = json.loads(self.ws.recv())
            if data.get("id") == mid:
                if "error" in data:
                    raise RuntimeError(data["error"].get("message", "cdp error"))
                return data.get("result", {})
        raise TimeoutError(method)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


DEFAULT_DIR_FOR_EXE = {"chrome.exe": USER_DATA_DIRS[1], "msedge.exe": USER_DATA_DIRS[2],
                       "brave.exe": USER_DATA_DIRS[3], "chromium.exe": USER_DATA_DIRS[4]}


def _endpoint(user_data_dir: Path) -> str | None:
    try:
        port, path = (user_data_dir / "DevToolsActivePort").read_text().split("\n")[:2]
        return f"ws://127.0.0.1:{port.strip()}{path.strip()}"
    except Exception:
        return None


def user_data_dir_for(pid: int, exe: str) -> Path | None:
    """Which browser profile directory a window's process uses."""
    try:
        import psutil

        for arg in psutil.Process(pid).cmdline():
            if arg.startswith("--user-data-dir="):
                return Path(arg.split("=", 1)[1].strip('"'))
    except Exception:
        pass
    return DEFAULT_DIR_FOR_EXE.get(exe.lower())


class Browsers:
    """Finds the CDP page that matches a browser window and runs the extractor in it."""

    def __init__(self):
        self.conns: dict[str, CDP] = {}
        self.dead_until: dict[str, float] = {}
        self.next_id = 1

    allow_real_profile = True

    def _connection(self, pid: int, exe: str) -> list[CDP]:
        d = user_data_dir_for(pid, exe)
        if d is None:
            return []
        if Path(d).resolve() != PROFILE_DIR.resolve() and not self.allow_real_profile:
            return []
        url = _endpoint(Path(d))
        if not url:
            return []
        c = self.conns.get(url)
        if c is None:
            if self.dead_until.get(url, 0) > time.time():
                return []
            try:
                c = CDP(url)  # your own Chrome may show "Allow remote debugging?" once per DeskPilot session
                self.conns[url] = c
            except Exception:
                self.dead_until[url] = time.time() + 30
                return []
        return [c]

    def page_for(self, window_title: str, pid: int = 0, exe: str = "chrome.exe"):
        """Return (cdp, sessionId, targetInfo) for the tab shown in this browser window, or None."""
        for c in self._connection(pid, exe):
            try:
                infos = c.call("Target.getTargets")["targetInfos"]
            except Exception:
                self.conns.pop(c.url, None)
                c.close()
                continue
            pages = [t for t in infos if t["type"] == "page" and not t["url"].startswith("devtools://")]
            match = None
            for t in sorted(pages, key=lambda t: -len(t["title"])):
                if t["title"] and window_title.startswith(t["title"]):
                    match = t
                    break
            if not match:
                continue
            sid = c.sessions.get(match["targetId"])
            if not sid:
                try:
                    sid = c.call("Target.attachToTarget", {"targetId": match["targetId"], "flatten": True})["sessionId"]
                except Exception:
                    continue
                c.sessions[match["targetId"]] = sid
            return c, sid, match
        return None

    def find_tab(self, url: str, windows) -> tuple | None:
        """(cdp, targetInfo) of an open tab already showing `url` in any of these browser windows, else None.
        Only uses browser connections that need no new "Allow remote debugging?" prompt: ones already open in this
        session, or the DeskPilot browser profile."""
        seen = set()
        for w in windows:
            d = user_data_dir_for(w.pid, w.exe())
            ep = _endpoint(Path(d)) if d else None
            if not ep or (ep not in self.conns and Path(d).resolve() != PROFILE_DIR.resolve()):
                continue
            for c in self._connection(w.pid, w.exe()):
                if c.url in seen:
                    continue
                seen.add(c.url)
                try:
                    infos = c.call("Target.getTargets")["targetInfos"]
                except Exception:
                    continue
                for t in infos:
                    if t["type"] == "page" and same_page(url, t["url"]):
                        return c, t
        return None

    def evaluate(self, page, expr: str, timeout: float = 10):
        c, sid, t = page
        try:
            r = c.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True},
                       session=sid, timeout=timeout)
        except RuntimeError:
            c.sessions.pop(t["targetId"], None)  # session went away (navigation/crash)
            raise
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"].get("text", "js error"))
        return r.get("result", {}).get("value")

    def extract(self, page, full: bool, filt: str | None, limit: int):
        expr = f"({EXTRACT_JS})({self.next_id}, {json.dumps(full)}, {json.dumps(filt)}, {limit})"
        res = self.evaluate(page, expr)
        self.next_id = max(self.next_id, res.get("next", self.next_id))
        return res

    def locate(self, page, ref: str):
        return self.evaluate(page, f"({LOCATE_JS})({json.dumps(ref)})")


def _url_key(u: str) -> tuple[str, str, str]:
    from urllib.parse import urlsplit

    p = urlsplit(u if "://" in u else "https://" + u)
    host = p.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    return host, p.path.rstrip("/").lower(), p.query


def same_page(want: str, have: str) -> bool:
    """Same site + path (ignores scheme, www., trailing slash and #fragment); a query in `want` must match."""
    if not have.startswith(("http://", "https://")):
        return False
    h1, p1, q1 = _url_key(want)
    h2, p2, q2 = _url_key(have)
    return h1 == h2 and p1 == p2 and (not q1 or q1 == q2)


def find_chrome() -> str | None:
    for p in (Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
              Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
              Path(_LA) / "Google/Chrome/Application/chrome.exe",
              Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe"):
        if p.exists():
            return str(p)
    return shutil.which("chrome") or shutil.which("msedge")


def launch_agent_browser(url: str = "about:blank") -> str:
    exe = find_chrome()
    if not exe:
        raise RuntimeError("Chrome/Edge not found")
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([exe, f"--user-data-dir={PROFILE_DIR}", "--remote-debugging-port=0", "--no-first-run",
                      "--no-default-browser-check", "--new-window", url], creationflags=0x08000000)
    return exe


# Runs in the page. Collects visible interactive elements + headings + short texts, flat, in DOM order.
EXTRACT_JS = r"""
function(start, full, filt, limit) {
  const W = window;
  W.__dpn = Math.max(W.__dpn || 0, start);
  W.__dpm = W.__dpm || new Map();
  const vw = innerWidth, vh = innerHeight;
  const out = []; let more = 0, offCount = 0;
  const f = filt ? filt.toLowerCase() : null;
  const INTER = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[contenteditable=""],[contenteditable=true],' +
    '[role=button],[role=link],[role=tab],[role=menuitem],[role=menuitemcheckbox],[role=menuitemradio],[role=option],' +
    '[role=checkbox],[role=radio],[role=switch],[role=combobox],[role=textbox],[role=searchbox],[role=slider],' +
    '[role=treeitem],[role=row],[onclick],[tabindex]:not([tabindex="-1"])';
  const roleOf = (el) => {
    const r = el.getAttribute('role'); if (r) return r;
    const t = el.tagName.toLowerCase();
    if (t === 'a') return 'link';
    if (t === 'button' || t === 'summary') return 'button';
    if (t === 'select') return 'combo';
    if (t === 'textarea') return 'edit';
    if (t === 'input') { const ty = (el.type || 'text').toLowerCase();
      return {checkbox:'checkbox', radio:'radio', submit:'button', button:'button', reset:'button', range:'slider',
              file:'file', image:'button'}[ty] || 'edit'; }
    if (/^h[1-6]$/.test(t)) return 'heading';
    if (el.isContentEditable) return 'edit';
    return 'clickable';
  };
  const clean = (s, n) => { s = (s || '').replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
  const nameOf = (el) => {
    let s = el.getAttribute('aria-label');
    if (!s && el.getAttribute('aria-labelledby')) s = el.getAttribute('aria-labelledby').split(' ')
        .map(id => { const e = el.ownerDocument.getElementById(id); return e ? e.innerText : ''; }).join(' ');
    if (!s && el.labels && el.labels.length) s = el.labels[0].innerText;
    if (!s && el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA' && el.tagName !== 'SELECT') s = el.innerText;
    if (!s) s = el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('alt') || '';
    if (!s) { const img = el.querySelector && el.querySelector('img[alt],svg title'); if (img) s = img.getAttribute ? (img.getAttribute('alt') || img.textContent) : ''; }
    return clean(s, 80);
  };
  const SENSITIVE = /cc-|card|cvc|cvv|iban|password|one-time-code/i;
  const valueOf = (el) => {
    const t = el.tagName;
    if (t === 'INPUT') {
      const ty = (el.type || '').toLowerCase();
      if (ty === 'password' || SENSITIVE.test(el.autocomplete || '') || SENSITIVE.test(el.name || '')) return el.value ? '***' : '';
      if (ty === 'checkbox' || ty === 'radio') return null;
      return clean(el.value, 60);
    }
    if (t === 'TEXTAREA') return clean(el.value, 60);
    if (t === 'SELECT') return clean(el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : '', 40);
    return null;
  };
  const visible = (el, r) => {
    if (r.width < 2 || r.height < 2) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none' && +cs.opacity > 0.05;
  };
  const seen = new Set();
  const covered = [];   // rects of emitted interactive elements (to skip their inner texts)
  const add = (el, role, ox, oy) => {
    if (seen.has(el)) return; seen.add(el);
    const r = el.getBoundingClientRect();
    if (!visible(el, r)) return;
    const x = r.left + ox, y = r.top + oy;
    const inView = x + r.width > 0 && y + r.height > 0 && x < vw && y < vh;
    if (!inView) { offCount++; if (!full && !f) return; }
    const name = role === 'text' ? clean(el.innerText, 100) : nameOf(el);
    const value = role === 'text' ? null : valueOf(el);
    if (!name && value == null && role !== 'edit') return;
    if (role === 'text' && !name) return;
    if (f && !(name.toLowerCase().includes(f) || String(value || '').toLowerCase().includes(f))) return;
    if (out.length >= limit) { more++; return; }
    if (!el.__dpid) { el.__dpid = 'b' + (W.__dpn++); }
    W.__dpm.set(el.__dpid, el);
    const flags = [];
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') flags.push('disabled');
    const ch = el.checked != null && (el.type === 'checkbox' || el.type === 'radio') ? el.checked : el.getAttribute('aria-checked');
    if (ch === true || ch === 'true') flags.push('[x]'); else if (ch === false || ch === 'false') flags.push('[ ]');
    if (el.getAttribute('aria-selected') === 'true') flags.push('selected');
    const ex = el.getAttribute('aria-expanded'); if (ex) flags.push(ex === 'true' ? 'expanded' : 'collapsed');
    if (el.getRootNode().activeElement === el) flags.push('*focus');
    if (el.required) flags.push('required');
    if (!inView) flags.push(y >= vh ? 'below' : y < 0 ? 'above' : 'aside');
    if (role === 'edit' && el.placeholder && name !== clean(el.placeholder, 80)) flags.push('placeholder="' + clean(el.placeholder, 40) + '"');
    out.push({id: el.__dpid, role, name, value, x, y, w: r.width, h: r.height, flags: flags.join(' ')});
    if (role !== 'text' && role !== 'heading') covered.push(el);
  };
  const walk = (root, ox, oy) => {
    const els = root.querySelectorAll('*');
    for (const el of els) {
      if (out.length >= limit && !f && !full) { more++; continue; }
      if (el.matches(INTER)) { add(el, roleOf(el), ox, oy); }
      else if (/^H[1-6]$/.test(el.tagName) || el.getAttribute('role') === 'heading') add(el, 'heading', ox, oy);
      else if (el.childElementCount === 0 || [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim().length > 1)) {
        const tx = (el.innerText || '').trim();
        if (tx.length > 1 && tx.length < 400 && !['SCRIPT','STYLE','NOSCRIPT','OPTION','svg'].includes(el.tagName)
            && !covered.some(c => c.contains(el)) && !(el.parentElement && seen.has(el.parentElement) && el.parentElement.innerText.trim() === tx))
          add(el, 'text', ox, oy);
      }
      if (el.shadowRoot) walk(el.shadowRoot, ox, oy);
      if (el.tagName === 'IFRAME') {
        try { const d = el.contentDocument; if (d) { const r = el.getBoundingClientRect(); walk(d, ox + r.left, oy + r.top); } } catch (e) {}
      }
    }
  };
  walk(document, 0, 0);
  return {items: out, more, off: offCount, vw, vh, title: document.title, url: location.href, next: W.__dpn,
          scrollY: Math.round(scrollY), scrollH: document.documentElement.scrollHeight};
}
"""

LOCATE_JS = r"""
function(id) {
  // Where to click element `id` right now: the middle of the part of it that is actually visible (not clipped by
  // a scrolled list, dropdown or the viewport) and not covered by something else. Scrolls it into view if needed.
  const el = window.__dpm && window.__dpm.get(id);
  if (!el || !el.isConnected) return null;
  const off = () => { let ox = 0, oy = 0, w = el.ownerDocument.defaultView;
    while (w && w.frameElement) { const fr = w.frameElement.getBoundingClientRect(); ox += fr.left; oy += fr.top; w = w.parent; }
    return [ox, oy]; };
  const visibleRect = () => {     // element rect clipped by every scrolling/clipping ancestor and the viewport
    const r = el.getBoundingClientRect();
    let l = r.left, t = r.top, rt = r.right, b = r.bottom;
    const view = el.ownerDocument.defaultView;
    for (let p = el.parentElement; p; p = p.parentElement) {
      const cs = view.getComputedStyle(p);
      if (/(auto|scroll|hidden|clip)/.test(cs.overflow + cs.overflowX + cs.overflowY)) {
        const pr = p.getBoundingClientRect();
        l = Math.max(l, pr.left); t = Math.max(t, pr.top); rt = Math.min(rt, pr.right); b = Math.min(b, pr.bottom);
      }
    }
    l = Math.max(l, 0); t = Math.max(t, 0);
    rt = Math.min(rt, view.innerWidth); b = Math.min(b, view.innerHeight);
    return {r, l, t, rt, b, ok: rt - l >= 2 && b - t >= 2 && (rt - l) * (b - t) >= 0.5 * Math.max(1, r.width * r.height)};
  };
  const hits = (x, y) => {      // would a click at (x, y) (element's own document coords) reach el?
    const h = el.ownerDocument.elementFromPoint(x, y);
    if (!h) return false;
    if (h === el || el.contains(h)) return true;
    if (h.contains(el) && h.tagName !== 'BODY' && h.tagName !== 'HTML') return true;
    const lab = h.closest && h.closest('label');
    return !!(lab && lab.control === el);
  };
  const point = (v) => {        // first spot inside the visible part that really hits el
    const cx = (v.l + v.rt) / 2, cy = (v.t + v.b) / 2, dx = (v.rt - v.l) / 4, dy = (v.b - v.t) / 4;
    for (const [x, y] of [[cx, cy], [cx - dx, cy], [cx + dx, cy], [cx, cy - dy], [cx, cy + dy], [v.l + 3, cy]])
      if (hits(x, y)) return [x, y];
    return null;
  };
  let v = visibleRect(), p = v.ok ? point(v) : null;
  if (!p) {
    el.scrollIntoView({block: 'nearest', inline: 'nearest', behavior: 'instant'});   // scrolls inner lists too
    v = visibleRect(); p = v.ok ? point(v) : null;
    if (!p) { el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'}); v = visibleRect(); p = point(v); }
  }
  const [ox, oy] = off();
  let cover = '';
  if (!p) {
    const h = el.ownerDocument.elementFromPoint((v.l + v.rt) / 2, (v.t + v.b) / 2);
    if (h) cover = (h.getAttribute('aria-label') || h.innerText || h.tagName).replace(/\s+/g, ' ').trim().slice(0, 50);
    p = [(v.l + v.rt) / 2, (v.t + v.b) / 2];
  }
  return {cx: p[0] + ox, cy: p[1] + oy, x: v.r.left + ox, y: v.r.top + oy, w: v.r.width, h: v.r.height,
          vw: innerWidth, vh: innerHeight, sy: scrollY, covered: cover, clipped: !v.ok};
}
"""
