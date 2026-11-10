"""Keep private data away from the AI: redact secrets in text, hide private windows, black out regions in images."""
from __future__ import annotations

import re

DEFAULT_PRIVATE_WINDOWS = ["1password", "bitwarden", "keepass", "lastpass", "dashlane", "proton pass",
                           "windows security", "credential manager", "authenticator",
                           "allow remote debugging", "user account control"]

_PATTERNS = [
    # API keys / tokens
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|sk_live_[A-Za-z0-9]{16,}|rk_live_[A-Za-z0-9]{16,}|"
                r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|xox[abpr]-[A-Za-z0-9-]{10,}|"
                r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|ya29\.[0-9A-Za-z_-]{20,})\b"), "[secret]"),
    # JWT
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "[token]"),
    # Private keys
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----|$)"), "[private key]"),
    # IBAN
    (re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b"), "[iban]"),
]
_CARD = re.compile(r"(?<![\d.])(?:\d[ -]?){12,18}\d(?![\d.])")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def redact(text: str) -> str:
    if not text:
        return text
    for pat, rep in _PATTERNS:
        text = pat.sub(rep, text)

    def card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return "[card]" if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)

    return _CARD.sub(card, text)


class Privacy:
    def __init__(self, rules: dict):
        words = rules.get("private_windows", DEFAULT_PRIVATE_WINDOWS) or []
        self.words = [w.lower() for w in words if w]
        self.enabled = rules.get("redact_secrets", True)

    def is_private(self, title: str, exe: str = "") -> bool:
        t, e = title.lower(), exe.lower()
        return any(w in t or w in e for w in self.words)

    def text(self, s: str) -> str:
        return redact(s) if self.enabled else s


def black_out(img, area, rects):
    """Fill rects (absolute screen coords) with black on an image of `area`."""
    from PIL import ImageDraw

    d = ImageDraw.Draw(img)
    for (l, t, r, b) in rects:
        d.rectangle((l - area.left, t - area.top, r - area.left, b - area.top), fill=(0, 0, 0))
    return img
