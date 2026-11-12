"""Screen perception: UI Automation tree -> compact text with stable refs, diffs, OCR, screenshots."""
from __future__ import annotations

import io
from dataclasses import dataclass, field

from .config import Rect

# ---------------------------------------------------------------- data model

ROLES = {
    50000: "button", 50001: "calendar", 50002: "checkbox", 50003: "combo", 50004: "edit", 50005: "link",
    50006: "image", 50007: "item", 50008: "list", 50009: "menu", 50010: "menubar", 50011: "menuitem",
    50012: "progress", 50013: "radio", 50014: "scrollbar", 50015: "slider", 50016: "spinner",
    50017: "statusbar", 50018: "tabs", 50019: "tab", 50020: "text", 50021: "toolbar", 50022: "tooltip",
    50023: "tree", 50024: "treeitem", 50025: "custom", 50026: "group", 50027: "thumb", 50028: "grid",
    50029: "row", 50030: "doc", 50031: "split", 50032: "window", 50033: "pane", 50034: "header",
    50035: "headeritem", 50036: "table", 50037: "titlebar", 50038: "separator", 50039: "zoom", 50040: "appbar",
}
INTERACTIVE = {"button", "checkbox", "combo", "edit", "link", "item", "menuitem", "radio", "slider", "spinner",
               "tab", "treeitem", "row", "split", "headeritem", "doc"}
SKIP_SUBTREE = {"scrollbar", "thumb", "separator", "titlebar", "tooltip", "progress"}
EDITABLE = {"edit", "doc", "combo"}


@dataclass
class Node:
    role: str
    name: str = ""
    value: str | None = None
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)  # l, t, r, b
    offscreen: bool = False
    enabled: bool = True
    focused: bool = False
    password: bool = False
    aid: str = ""
    toggled: int | None = None        # 0 off, 1 on, 2 indeterminate
    expanded: int | None = None       # 0 collapsed, 1 expanded, 2 partial, 3 leaf
    selected: bool | None = None
    children: list["Node"] = field(default_factory=list)

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.rect
        return (l + r) // 2, (t + b) // 2

    @property
    def visible(self) -> bool:
        l, t, r, b = self.rect
        return not self.offscreen and r - l > 1 and b - t > 1


@dataclass
class RefInfo:
    ref: str
    role: str
    name: str
    rect: tuple[int, int, int, int]
    editable: bool = False
    password: bool = False

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.rect
        return (l + r) // 2, (t + b) // 2


class RefTable:
    """Stable short refs: the same element (role, name, id, approx position) keeps its ref across snapshots."""

    def __init__(self, prefix: str = "e"):
        self.prefix = prefix
        self.by_sig: dict[tuple, str] = {}
        self.info: dict[str, RefInfo] = {}
        self.n = 0

    def assign(self, role: str, name: str, aid: str, rect, editable=False, password=False) -> str:
        sig = (role, name, aid, rect[0] // 6, rect[1] // 6, (rect[2] - rect[0]) // 6)
        ref = self.by_sig.get(sig)
        if ref is None:
            if self.n > 20000:
                self.by_sig.clear()
                self.info.clear()
            self.n += 1
            ref = f"{self.prefix}{self.n}"
            self.by_sig[sig] = ref
        self.info[ref] = RefInfo(ref, role, name, tuple(rect), editable, password)
        return ref

    def get(self, ref: str) -> RefInfo | None:
        return self.info.get(ref)


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def describe(node: Node, name_len: int = 80) -> str:
    """'button "OK"' style description without the ref."""
    parts = []
    if node.role != "text":
        parts.append(node.role)
    if node.name:
        parts.append(f'"{_clip(node.name, name_len)}"')
    if node.value not in (None, "") and node.value != node.name:
        parts.append(f'="{_clip(str(node.value), 40)}"' if not node.password else "=***")
    if node.toggled == 1:
        parts.append("[x]")
    elif node.toggled == 0 and node.role in ("checkbox", "button", "item", "menuitem"):
        parts.append("[ ]")
    if node.expanded == 0:
        parts.append("collapsed")
    elif node.expanded == 1:
        parts.append("expanded")
    if node.selected:
        parts.append("selected")
    if node.focused:
        parts.append("*focus")
    if not node.enabled:
        parts.append("disabled")
    if node.password:
        parts.append("password")
    return " ".join(parts)


def format_tree(root: Node, areas: list[Rect], refs: RefTable, max_nodes: int = 250,
                filt: str | None = None) -> tuple[list[str], dict[str, str], int]:
    """Return (lines, ref->line, hidden_count). Unnamed wrappers are flattened; offscreen and tiny nodes dropped."""
    lines: list[str] = []
    by_ref: dict[str, str] = {}
    hidden = 0
    f = filt.lower() if filt else None

    def on_screen(n: Node) -> bool:
        l, t, r, b = n.rect
        return n.visible and any(a.intersects(l, t, r, b) for a in areas)

    def emit(n: Node, depth: int, parent_name: str) -> int:
        nonlocal hidden
        if n.role in SKIP_SUBTREE:
            return depth
        show = False
        if on_screen(n):
            if n.role in INTERACTIVE:
                show = True
            elif n.name and n.name != parent_name and not parent_name.startswith(n.name[:40]):
                show = True
        if show and n.role == "text" and parent_name and n.name in parent_name:
            show = False
        if show and f and f not in (n.name or "").lower() and f not in str(n.value or "").lower():
            show = False
        if show:
            if len(lines) >= max_nodes:
                hidden += 1
            else:
                ref = refs.assign(n.role, n.name, n.aid, n.rect, n.role in EDITABLE, n.password)
                line = f"{' ' * min(depth, 8)}{ref} {describe(n)}"
                lines.append(line)
                by_ref[ref] = line.strip()
        child_depth = depth + 1 if show and not f else depth
        pname = n.name if show else parent_name
        for c in n.children:
            emit(c, child_depth, pname)
        return depth

    # The window itself is already named in the snapshot header; start with its children.
    for c in root.children:
        emit(c, 0, root.name)
    return lines, by_ref, hidden


def diff(old: dict[str, str], new: dict[str, str]) -> tuple[list[str], list[str]]:
    added = [line for ref, line in new.items() if old.get(ref) != line]
    removed = [line for ref, line in old.items() if ref not in new]
    return added, removed


def find_nodes(root: Node, text: str | None, role: str | None, areas: list[Rect], onscreen: bool = True) -> list[Node]:
    t = (text or "").lower()
    out: list[Node] = []

    def walk(n: Node):
        if n.role in SKIP_SUBTREE:
            return
        l, tp, r, b = n.rect
        if not onscreen or (n.visible and any(a.intersects(l, tp, r, b) for a in areas)):
            ok = True
            if t and t not in (n.name or "").lower() and t not in str(n.value or "").lower() and t != n.aid.lower():
                ok = False
            if role and n.role != role:
                ok = False
            if ok and (n.name or n.role in INTERACTIVE):
                out.append(n)
        for c in n.children:
            walk(c)

    walk(root)
    # Prefer interactive elements and exact matches.
    out.sort(key=lambda n: (n.role not in INTERACTIVE, (n.name or "").lower() != t, len(n.name or "")))
    return out


# ---------------------------------------------------------------- UIA (COM)

P_NAME, P_TYPE, P_RECT, P_OFF, P_PWD, P_AID, P_ENABLED, P_FOCUS = 30005, 30003, 30001, 30022, 30019, 30011, 30010, 30008
P_VALUE, P_TOGGLE, P_EXPAND, P_SELECTED = 30045, 30086, 30070, 30079


class UIA:
    """Must be created and used on a single thread."""

    def __init__(self):
        import uiautomation as auto  # noqa: F401  (initialises COM on this thread)
        from uiautomation.uiautomation import _AutomationClient

        self.uia = _AutomationClient.instance().IUIAutomation
        self.cr = self.uia.CreateCacheRequest()
        for p in (P_NAME, P_TYPE, P_RECT, P_OFF, P_PWD, P_AID, P_ENABLED, P_FOCUS, P_VALUE, P_TOGGLE, P_EXPAND,
                  P_SELECTED):
            self.cr.AddProperty(p)
        self.cr.TreeScope = 7  # element + children + descendants

    @staticmethod
    def _prop(el, pid):
        try:
            v = el.GetCachedPropertyValue(pid)
        except Exception:
            return None
        return v if isinstance(v, (str, int, bool, float)) else None

    def _convert(self, el, depth: int = 0) -> Node:
        try:
            r = el.CachedBoundingRectangle
            rect = (r.left, r.top, r.right, r.bottom)
        except Exception:
            rect = (0, 0, 0, 0)
        role = ROLES.get(el.CachedControlType, "custom")
        n = Node(
            role=role,
            name=el.CachedName or "",
            value=self._prop(el, P_VALUE),
            rect=rect,
            offscreen=bool(el.CachedIsOffscreen),
            enabled=bool(el.CachedIsEnabled),
            focused=bool(el.CachedHasKeyboardFocus),
            password=bool(el.CachedIsPassword),
            aid=el.CachedAutomationId or "",
            toggled=self._prop(el, P_TOGGLE),
            expanded=self._prop(el, P_EXPAND) if role in ("combo", "treeitem", "menuitem") else None,
            selected=self._prop(el, P_SELECTED) if role in ("item", "tab", "treeitem", "row", "radio") else None,
        )
        if isinstance(n.value, (int, float, bool)):
            n.value = str(n.value)
        if depth < 60:
            kids = el.GetCachedChildren()
            if kids:
                for i in range(kids.Length):
                    n.children.append(self._convert(kids.GetElement(i), depth + 1))
        return n

    def tree(self, hwnd: int) -> Node:
        el = self.uia.ElementFromHandle(hwnd)
        return self._convert(el.BuildUpdatedCache(self.cr))

    def address_bar(self, hwnd: int) -> str | None:
        """The URL in a Chromium browser window's address bar (its active tab), via accessibility. No CDP needed."""
        try:
            el = self.uia.ElementFromHandle(hwnd)
            bar = el.FindFirst(4, self.uia.CreatePropertyCondition(P_NAME, "Address and search bar"))  # descendants
            if not bar:
                return None
            v = bar.GetCurrentPropertyValue(P_VALUE)
            return str(v) if v else None
        except Exception:
            return None

    def focused(self) -> Node | None:
        try:
            el = self.uia.GetFocusedElement()
            cr = self.uia.CreateCacheRequest()
            for p in (P_NAME, P_TYPE, P_RECT, P_OFF, P_PWD, P_AID, P_ENABLED, P_FOCUS, P_VALUE):
                cr.AddProperty(p)
            el = el.BuildUpdatedCache(cr)
            r = el.CachedBoundingRectangle
            return Node(role=ROLES.get(el.CachedControlType, "custom"), name=el.CachedName or "",
                        rect=(r.left, r.top, r.right, r.bottom), password=bool(el.CachedIsPassword),
                        value=self._prop(el, P_VALUE))
        except Exception:
            return None

    def relocate(self, x: int, y: int, role: str, name: str, old_rect) -> tuple[int, int, int, int] | None:
        """Where an element seen at `old_rect` is NOW, so a click lands on it even after the page scrolled, a list
        expanded or the layout shifted. Returns a fresh rect whose center really shows that element (scrolling it
        into view inside its list/page if needed), or None if it can't be found."""
        if self._hits(x, y, name, old_rect):
            return None  # still where it was: keep the snapshot rect
        hwnd = _root_hwnd_at(x, y)
        try:
            import win32gui

            fg = win32gui.GetForegroundWindow()
        except Exception:
            fg = 0
        types = [t for t, r in ROLES.items() if r == role]
        best, best_d = None, None
        for h in dict.fromkeys(h for h in (fg, hwnd) if h):
            try:
                root = self.uia.ElementFromHandle(h)
                cond = self.uia.CreatePropertyCondition(P_NAME, name)
                if types:
                    cond = self.uia.CreateAndCondition(cond, self.uia.CreatePropertyCondition(P_TYPE, types[0]))
                found = root.FindAll(4, cond)  # descendants
            except Exception:
                continue
            for i in range(found.Length if found else 0):
                el = found.GetElement(i)
                try:
                    r = el.CurrentBoundingRectangle
                except Exception:
                    continue
                if r.right - r.left < 2 or r.bottom - r.top < 2:
                    continue
                d = abs((r.left + r.right) / 2 - (old_rect[0] + old_rect[2]) / 2) + \
                    abs((r.top + r.bottom) / 2 - (old_rect[1] + old_rect[3]) / 2)
                if best_d is None or d < best_d:
                    best, best_d = el, d
        if best is None:
            return None
        rect = self._rect(best)
        cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
        if not self._hits(cx, cy, name, rect):
            try:  # clipped by its list / scrolled away: ask the app to bring it into view
                pat = best.GetCurrentPattern(10017)  # ScrollItemPattern
                if pat:
                    from uiautomation.uiautomation import _AutomationClient

                    pat.QueryInterface(_AutomationClient.instance().UIAutomationCore.IUIAutomationScrollItemPattern
                                       ).ScrollIntoView()
                    import time

                    time.sleep(0.25)
                    rect = self._rect(best)
            except Exception:
                pass
        return rect

    @staticmethod
    def _rect(el) -> tuple[int, int, int, int]:
        r = el.CurrentBoundingRectangle
        return r.left, r.top, r.right, r.bottom

    def _hits(self, x: int, y: int, name: str, rect) -> bool:
        """Does a click at (x, y) land on the element (name, rect)? True when the element under the point is it,
        or something inside it (a label in a button), or it is inside what's under the point."""
        n = self.at_point(x, y)
        if not n:
            return False
        l, t, r, b = rect
        nl, nt, nr, nb = n.rect
        inside = nl >= l - 3 and nt >= t - 3 and nr <= r + 3 and nb <= b + 3
        around = nl <= l + 3 and nt <= t + 3 and nr >= r - 3 and nb >= b - 3
        return (inside and (not name or not n.name or n.name == name or name in n.name or n.name in name)) or (
            around and n.name == name and bool(name))

    def at_point(self, x: int, y: int) -> Node | None:
        try:
            from ctypes import wintypes

            el = self.uia.ElementFromPoint(wintypes.POINT(x, y))
            cr = self.uia.CreateCacheRequest()
            for p in (P_NAME, P_TYPE, P_RECT):
                cr.AddProperty(p)
            el = el.BuildUpdatedCache(cr)
            r = el.CachedBoundingRectangle
            return Node(role=ROLES.get(el.CachedControlType, "custom"), name=el.CachedName or "",
                        rect=(r.left, r.top, r.right, r.bottom))
        except Exception:
            return None


# ---------------------------------------------------------------- pixels

def grab(area: Rect):
    import mss
    from PIL import Image

    with mss.MSS() as s:
        shot = s.grab({"left": area.left, "top": area.top, "width": area.width, "height": area.height})
    return Image.frombytes("RGB", shot.size, shot.rgb)


@dataclass
class OcrLine:
    text: str
    rect: tuple[int, int, int, int]
    words: list[tuple[str, tuple[int, int, int, int]]]


def ocr(area: Rect, img=None) -> list[OcrLine]:
    """Windows built-in OCR (no install). Returns lines in absolute screen coordinates."""
    import asyncio

    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

    img = img or grab(area)
    scale = 1.0
    max_dim = OcrEngine.max_image_dimension
    if max(img.size) > max_dim:
        scale = max_dim / max(img.size)
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    rgba = img.convert("RGBA")
    w = DataWriter()
    w.write_bytes(rgba.tobytes("raw", "BGRA"))
    bmp = SoftwareBitmap.create_copy_from_buffer(w.detach_buffer(), BitmapPixelFormat.BGRA8, rgba.width,
                                                 rgba.height, BitmapAlphaMode.PREMULTIPLIED)
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("No OCR language installed (Settings > Time & language > Language).")

    async def run():
        return await engine.recognize_async(bmp)

    res = asyncio.run(run())
    out: list[OcrLine] = []
    for line in res.lines:
        words = []
        for wd in line.words:
            r = wd.bounding_rect
            l, t = area.left + int(r.x / scale), area.top + int(r.y / scale)
            words.append((wd.text, (l, t, l + int(r.width / scale), t + int(r.height / scale))))
        if not words:
            continue
        rect = (min(w[1][0] for w in words), min(w[1][1] for w in words),
                max(w[1][2] for w in words), max(w[1][3] for w in words))
        out.append(OcrLine(line.text, rect, words))
    return out


def ocr_match(lines: list[OcrLine], text: str) -> list[tuple[str, tuple[int, int, int, int]]]:
    """Find text (one or more words) in OCR output; returns (matched text, rect) using word-level boxes."""
    q = text.lower().split()
    hits = []
    if not q:
        return hits
    for ln in lines:
        ws = [w[0].lower() for w in ln.words]
        for i in range(len(ws) - len(q) + 1):
            if all(q[j] in ws[i + j] for j in range(len(q))):
                boxes = [ln.words[i + j][1] for j in range(len(q))]
                rect = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes),
                        max(b[3] for b in boxes))
                hits.append((" ".join(ln.words[i + j][0] for j in range(len(q))), rect))
    return hits


def encode_jpeg(img, max_side: int = 1280, quality: int = 60) -> tuple[bytes, float]:
    scale = min(1.0, max_side / max(img.size))
    if scale < 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue(), scale


def draw_marks(img, area: Rect, refs: list[RefInfo]):
    from PIL import ImageDraw

    d = ImageDraw.Draw(img)
    for info in refs:
        l, t, r, b = info.rect
        l, t, r, b = l - area.left, t - area.top, r - area.left, b - area.top
        d.rectangle((l, t, r, b), outline=(255, 122, 26), width=2)
        d.rectangle((l, t - 14, l + 8 * len(info.ref) + 4, t), fill=(255, 122, 26))
        d.text((l + 2, t - 14), info.ref, fill=(0, 0, 0))
    return img


def _root_hwnd_at(x: int, y: int) -> int:
    try:
        import ctypes

        import win32gui

        h = win32gui.WindowFromPoint((x, y))
        return ctypes.windll.user32.GetAncestor(h, 2) if h else 0
    except Exception:
        return 0
