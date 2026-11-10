from deskpilot.config import Rect
from deskpilot.perceive import Node, OcrLine, RefTable, diff, find_nodes, format_tree, ocr_match
from deskpilot.policy import DEFAULT_POLICY, Gate, Policy, normalize_combo

AREA = [Rect(0, 0, 1920, 1080)]


def tree():
    return Node("window", "My App", rect=(0, 0, 1920, 1080), children=[
        Node("pane", "", rect=(0, 0, 1920, 1080), children=[
            Node("button", "Install", rect=(10, 10, 110, 40), children=[Node("text", "Install", rect=(12, 12, 100, 38))]),
            Node("button", "Delete file", rect=(120, 10, 220, 40)),
            Node("edit", "Search", value="cats", rect=(300, 10, 600, 40), focused=True),
            Node("button", "Offscreen", rect=(10, 2000, 100, 2040), offscreen=True),
            Node("button", "Other monitor", rect=(2000, 10, 2100, 40)),
            Node("scrollbar", "", rect=(1900, 0, 1920, 1080), children=[Node("button", "Line up", rect=(1900, 0, 1920, 20))]),
            Node("text", "Hello world", rect=(10, 100, 300, 120)),
        ]),
    ])


def test_format_flattens_and_filters():
    refs = RefTable()
    lines, by_ref, hidden = format_tree(tree(), AREA, refs)
    text = "\n".join(lines)
    assert 'button "Install"' in text
    assert text.count('"Install"') == 1          # duplicate child text dropped
    assert 'edit "Search" ="cats" *focus' in text
    assert "Offscreen" not in text and "Other monitor" not in text and "Line up" not in text
    assert '"Hello world"' in text and "text " not in text   # text role is implicit
    assert "pane" not in text and "My App" not in text       # unnamed wrapper + root flattened
    assert hidden == 0 and len(by_ref) == len(lines)


def test_refs_stable_and_diff():
    refs = RefTable()
    _, a, _ = format_tree(tree(), AREA, refs)
    t2 = tree()
    t2.children[0].children.append(Node("button", "About", rect=(10, 200, 100, 230)))
    t2.children[0].children.pop(1)  # Delete file removed
    _, b, _ = format_tree(t2, AREA, refs)
    shared = set(a) & set(b)
    assert len(shared) == len(a) - 1
    added, removed = diff(a, b)
    assert len(added) == 1 and "About" in added[0]
    assert len(removed) == 1 and "Delete file" in removed[0]


def test_max_nodes_and_filter():
    refs = RefTable()
    lines, _, hidden = format_tree(tree(), AREA, refs, max_nodes=2)
    assert len(lines) == 2 and hidden == 2
    lines, _, _ = format_tree(tree(), AREA, refs, filt="search")
    assert len(lines) == 1 and "Search" in lines[0]


def test_find_prefers_interactive_exact():
    nodes = find_nodes(tree(), "install", None, AREA)
    assert nodes[0].role == "button"


def test_ocr_match_multiword():
    ln = OcrLine("Open About this app", (0, 0, 300, 20), [
        ("Open", (0, 0, 50, 20)), ("About", (60, 0, 120, 20)), ("this", (130, 0, 170, 20)), ("app", (180, 0, 220, 20))])
    hits = ocr_match([ln], "about this")
    assert hits == [("About this", (60, 0, 170, 20))]


def test_policy_rules():
    p = Policy(dict(DEFAULT_POLICY))
    assert p.check({"kind": "click", "name": "About this app"}).level == "allow"
    assert p.check({"kind": "click", "name": "Delete file"}).level == "ask"
    assert p.check({"kind": "click", "name": "Buy now"}).level == "ask"
    assert p.check({"kind": "click", "name": "Posts"}).level == "allow"      # word boundary
    assert p.check({"kind": "key", "combo": "Delete", "editable": True}).level == "allow"
    assert p.check({"kind": "key", "combo": "delete", "editable": False}).level == "ask"
    assert p.check({"kind": "key", "combo": "shift+del"}).level == "ask"
    assert p.check({"kind": "key", "combo": "ctrl+l"}).level == "allow"
    assert p.check({"kind": "type", "password": True}).level == "ask"
    assert p.check({"kind": "launch", "app": "C:\\Windows\\System32\\cmd.exe"}).level == "ask"
    assert p.check({"kind": "launch", "app": "notepad"}).level == "allow"
    assert p.check({"kind": "click", "name": "Next"}, strict=True).level == "ask"
    p2 = Policy({**DEFAULT_POLICY, "deny_click_words": ["format"]})
    assert p2.check({"kind": "click", "name": "Format drive"}).level == "deny"


def test_gate_session_grant(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKPILOT_HOME", str(tmp_path))
    asked = []

    def fake(title, detail):
        asked.append(title)
        return "session"

    g = Gate(Policy(dict(DEFAULT_POLICY)), confirm=fake)
    assert g.require({"kind": "click", "name": "Delete"})[0]
    assert g.require({"kind": "click", "name": "Delete"})[0]
    assert len(asked) == 1
    g2 = Gate(Policy(dict(DEFAULT_POLICY)), confirm=lambda t, d: "deny")
    ok, msg = g2.require({"kind": "click", "name": "Delete"})
    assert not ok and "USER DENIED" in msg


def test_normalize_combo():
    assert normalize_combo("Shift + Ctrl + S") == "ctrl+shift+s"
    assert normalize_combo("Control+Del") == "ctrl+delete"


def test_redact_secrets():
    from deskpilot.privacy import redact

    s = "key sk-abcdefghijklmnopqrstuv card 4111 1111 1111 1111 order 1234567890123 jwt eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4"
    r = redact(s)
    assert "sk-abc" not in r and "[secret]" in r
    assert "[card]" in r and "1234567890123" in r          # non-Luhn number kept
    assert "[token]" in r


def test_private_windows():
    from deskpilot.privacy import Privacy

    p = Privacy({"private_windows": ["bitwarden", "bank"]})
    assert p.is_private("Bitwarden - Vault", "")
    assert p.is_private("x", "Bitwarden.exe")
    assert not p.is_private("Google Play Console", "chrome.exe")


def test_play_console_rules():
    p = Policy(dict(DEFAULT_POLICY))
    assert p.check({"kind": "click", "name": "Start rollout to Production"}).level == "ask"
    assert p.check({"kind": "click", "name": "Create app"}).level == "allow"


def test_routines_roundtrip(tmp_path, monkeypatch):
    from deskpilot import routines as R

    monkeypatch.setattr(R, "DIR", tmp_path)
    steps = [{"click": "Create app"}, {"type": "{{app_name}}", "into": "App name"}, {"key": "enter"}]
    d = R.save("New App", steps, "create an app in Play Console")
    assert d["params"] == ["app_name"]
    assert [r["name"] for r in R.all_routines()] == ["New App"]
    filled = R.fill(R.load("new app")["steps"], {"app_name": "Cool"})
    assert filled[1]["type"] == "Cool" and filled[0] == {"click": "Create app"}
    import pytest

    with pytest.raises(ValueError):
        R.fill(steps, {})
    assert R.delete("New App") and not R.all_routines()
