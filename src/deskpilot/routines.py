"""Saved routines: named, replayable step lists with {{placeholders}}. Stored in ~/.deskpilot/routines/."""
from __future__ import annotations

import json
import re
import time

from .config import HOME

DIR = HOME / "routines"
TARGET_KEYS = ("click", "dblclick", "rclick", "hover", "into", "at", "drag", "to")
_PARAM = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not s:
        raise ValueError("routine name is empty")
    return s


def params_of(steps: list) -> list[str]:
    found: list[str] = []
    for m in _PARAM.finditer(json.dumps(steps, ensure_ascii=False)):
        if m.group(1) not in found:
            found.append(m.group(1))
    return found


def fill(steps: list, values: dict) -> list:
    missing = [p for p in params_of(steps) if p not in values]
    if missing:
        raise ValueError(f"missing params: {', '.join(missing)}")

    def sub(v):
        if isinstance(v, str):
            return _PARAM.sub(lambda m: str(values[m.group(1)]), v)
        if isinstance(v, list):
            return [sub(x) for x in v]
        if isinstance(v, dict):
            return {k: sub(x) for k, x in v.items()}
        return v

    return sub(steps)


def save(name: str, steps: list, description: str = "") -> dict:
    DIR.mkdir(parents=True, exist_ok=True)
    data = {"name": name, "description": description, "params": params_of(steps), "steps": steps,
            "saved": time.strftime("%Y-%m-%d %H:%M"), "runs": 0}
    (DIR / f"{slug(name)}.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return data


def load(name: str) -> dict:
    p = DIR / f"{slug(name)}.json"
    if not p.exists():
        raise ValueError(f'no routine "{name}". Use routine(op="list").')
    return json.loads(p.read_text(encoding="utf-8"))


def bump(name: str) -> None:
    try:
        d = load(name)
        d["runs"] = d.get("runs", 0) + 1
        (DIR / f"{slug(name)}.json").write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def all_routines() -> list[dict]:
    if not DIR.exists():
        return []
    out = []
    for p in sorted(DIR.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    return out


def delete(name: str) -> bool:
    p = DIR / f"{slug(name)}.json"
    if p.exists():
        p.unlink()
        return True
    return False
