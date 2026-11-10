"""Permission rules: allow / ask / deny, the confirm dialog, and the audit log."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field

import yaml

from .config import AUDIT_FILE, HOME, POLICY_FILE

DEFAULT_POLICY = {
    # Click targets whose name matches any of these words need your OK.
    "ask_click_words": [
        "delete", "remove", "uninstall", "erase", "format", "wipe", "discard", "permanently",
        "pay", "buy", "purchase", "place order", "checkout", "subscribe", "donate", "transfer",
        "send", "post", "publish", "submit", "sign out", "log out", "logout", "reset", "factory",
        "empty recycle bin", "shut down", "restart", "install",
    ],
    # Key combos that need your OK (Delete only when focus is not a text field).
    "ask_keys": ["delete", "shift+delete", "alt+f4", "ctrl+w", "ctrl+shift+w"],
    # Programs that need your OK before launching.
    "ask_launch": ["cmd", "powershell", "pwsh", "regedit", "wt", "terminal", "taskmgr", "diskpart",
                   "mmc", "gpedit", "control"],
    "ask_close_window": True,
    "ask_password_typing": True,
    "ask_switch_screen": True,
    # Click targets matching these are always refused.
    "deny_click_words": [],
    "confirm_timeout_seconds": 60,
    # Privacy: windows whose title/app contains any of these are never read, captured or clicked.
    "private_windows": ["1password", "bitwarden", "keepass", "lastpass", "dashlane", "proton pass",
                        "windows security", "credential manager", "authenticator",
                        "allow remote debugging", "user account control"],
    # Read pages in your everyday Chrome/Edge too (needs chrome://inspect/#remote-debugging turned on;
    # Chrome asks you to Allow once per session). False = only the DeskPilot browser.
    "use_my_browser": True,
    # Replace API keys, tokens, card numbers, IBANs and private keys with placeholders before the AI sees them.
    "redact_secrets": True,
    # Website-specific words that always need your OK (e.g. Play Console publishing).
    # Hide the border / agent cursor / panel when the agent has made no call for this long (0 = never).
    # The agent also hides it right away by calling done() when a task is finished.
    "hide_overlay_after_idle_seconds": 30,
    "ask_click_words_extra": ["rollout", "roll out", "start rollout", "release to production", "unpublish",
                              "send for review", "delete app", "remove app"],
}


def load_policy() -> dict:
    HOME.mkdir(parents=True, exist_ok=True)
    if not POLICY_FILE.exists():
        POLICY_FILE.write_text(
            "# DeskPilot permission rules. Edit and restart the agent to apply.\n"
            + yaml.safe_dump(DEFAULT_POLICY, sort_keys=False)
        )
        return dict(DEFAULT_POLICY)
    try:
        user = yaml.safe_load(POLICY_FILE.read_text()) or {}
    except Exception:
        user = {}
    return {**DEFAULT_POLICY, **user}


def _word_re(words: list[str]) -> re.Pattern | None:
    words = [w for w in words if w]
    if not words:
        return None
    return re.compile(r"(?<![a-z])(" + "|".join(re.escape(w.lower()) for w in words) + r")(?![a-z])", re.I)


@dataclass
class Decision:
    level: str          # allow | ask | deny
    reason: str = ""
    key: str = ""       # identity used for "allow for session"


@dataclass
class Policy:
    rules: dict = field(default_factory=load_policy)

    def __post_init__(self):
        self._ask_click = _word_re(self.rules["ask_click_words"] + self.rules.get("ask_click_words_extra", []))
        self._deny_click = _word_re(self.rules["deny_click_words"])
        self._ask_keys = {normalize_combo(k) for k in self.rules["ask_keys"]}
        self._ask_launch = {a.lower() for a in self.rules["ask_launch"]}

    def check(self, a: dict, strict: bool = False) -> Decision:
        """a = {kind, name?, role?, combo?, password?, editable?, app?, window?}."""
        kind = a.get("kind")
        name = (a.get("name") or "").strip()
        if kind in ("click", "dblclick", "rclick", "drag") and name:
            if self._deny_click and self._deny_click.search(name):
                return Decision("deny", f"'{name}' is on your deny list")
            m = self._ask_click and self._ask_click.search(name)
            if m:
                return Decision("ask", f"click '{name}' ({m.group(1)})", f"click:{name.lower()}")
        if kind == "key":
            combo = normalize_combo(a.get("combo", ""))
            if combo in self._ask_keys:
                if combo == "delete" and a.get("editable"):
                    pass  # Delete inside a text box is just editing
                else:
                    target = f" on '{name}'" if name else ""
                    return Decision("ask", f"press {combo}{target}", f"key:{combo}")
        if kind == "type" and a.get("password") and self.rules["ask_password_typing"]:
            return Decision("ask", "type into a password field", "type:password")
        if kind == "launch":
            app = (a.get("app") or "").lower()
            base = re.split(r"[\\/]", app)[-1].removesuffix(".exe")
            if base in self._ask_launch:
                return Decision("ask", f"launch {app}", f"launch:{base}")
        if kind == "close" and self.rules["ask_close_window"]:
            return Decision("ask", f"close window '{a.get('window', '')}'", f"close:{a.get('window', '')}")
        if kind == "screen" and self.rules["ask_switch_screen"]:
            return Decision("ask", f"switch control to {a.get('name')}", "screen")
        if strict and kind not in ("read",):
            return Decision("ask", a.get("summary") or f"{kind} {name}".strip(), "")
        return Decision("allow")


_ALIASES = {"control": "ctrl", "del": "delete", "escape": "esc", "return": "enter", "windows": "win",
            "cmd": "win", "super": "win", "option": "alt"}
_MODS = ["ctrl", "shift", "alt", "win"]


def normalize_combo(combo: str) -> str:
    parts = [_ALIASES.get(p.strip().lower(), p.strip().lower()) for p in combo.replace(" ", "").split("+") if p]
    mods = [m for m in _MODS if m in parts]
    rest = [p for p in parts if p not in _MODS]
    return "+".join(mods + rest)


class Gate:
    """Applies a Policy, shows the confirm dialog when needed, remembers session grants, logs everything."""

    def __init__(self, policy: Policy | None = None, confirm=None):
        self.policy = policy or Policy()
        self.session_grants: set[str] = set()
        self._confirm = confirm or self._dialog

    def _dialog(self, title: str, detail: str) -> str:
        timeout = int(self.policy.rules.get("confirm_timeout_seconds", 60))
        try:
            out = subprocess.run([sys.executable, "-m", "deskpilot.confirm", title, detail, str(timeout)],
                                 capture_output=True, text=True, timeout=timeout + 15,
                                 creationflags=0x08000000)
            return out.stdout.strip() or "deny"
        except Exception:
            return "deny"

    def require(self, a: dict, strict: bool = False, detail: str = "") -> tuple[bool, str]:
        d = self.policy.check(a, strict)
        if d.level == "allow":
            return True, ""
        if d.level == "deny":
            audit({"event": "denied_by_rule", **a, "reason": d.reason})
            return False, f"DENIED by rule: {d.reason}"
        if d.key and d.key in self.session_grants:
            return True, ""
        ans = self._confirm(d.reason[:1].upper() + d.reason[1:], detail)
        audit({"event": "asked", **a, "reason": d.reason, "answer": ans})
        if ans == "session" and d.key:
            self.session_grants.add(d.key)
        if ans in ("once", "session"):
            return True, ""
        return False, f"USER DENIED: {d.reason}. Do not retry; ask the user what to do instead."


def audit(entry: dict) -> None:
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        entry = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **{k: v for k, v in entry.items() if k != "password"}}
        with AUDIT_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass
