import sys

import pytest

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


def test_same_page_matching():
    from deskpilot.browser import same_page

    assert same_page("https://github.com/iamtahiralvi/DeskPilot", "https://github.com/iamtahiralvi/DeskPilot/")
    assert same_page("github.com/iamtahiralvi/deskpilot", "https://www.github.com/iamtahiralvi/DeskPilot#readme")
    assert same_page("https://x.com/home", "https://x.com/home?ref=1")          # no query asked: any query is fine
    assert not same_page("https://x.com/home?tab=2", "https://x.com/home?tab=1")
    assert not same_page("https://github.com/iamtahiralvi", "https://github.com/iamtahiralvi/DeskPilot")
    assert not same_page("https://github.com", "chrome://newtab/")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows input")
def test_user_moved_detection(monkeypatch):
    from deskpilot import act

    inp = act.Input()
    assert not inp.user_moved()  # not tracking yet
    monkeypatch.setattr(act, "cursor_pos", lambda: (500, 500))
    inp.expect = (500, 510)
    assert not inp.user_moved()  # jitter is fine
    inp.expect = (100, 100)
    assert inp.user_moved()
    inp.grace(5)
    assert not inp.user_moved()  # grace after a dialog
    assert act.UserInterrupted().retry and not act.UserInterrupted("x", retry=False).retry


def test_ask_click_skips_passive_and_content_cards():
    p = Policy(rules=dict(DEFAULT_POLICY))
    assert p.check({"kind": "click", "name": "Post", "role": "button"}).level == "ask"
    assert p.check({"kind": "click", "name": "Create Post", "role": "link"}).level == "ask"
    assert p.check({"kind": "click", "name": "Delete", "role": ""}).level == "ask"
    assert p.check({"kind": "click", "name": "Who can post,", "role": "slider"}).level == "allow"
    assert p.check({"kind": "click", "name": "Actions available for this post", "role": "group"}).level == "allow"
    assert p.check({"kind": "click", "name": "Developer communications Used to send news", "role": "checkbox"}
                   ).level == "allow"
    long_link = "u/someone replied to your post in r/MusicTeachers My students have a homework journal 49m ago"
    assert p.check({"kind": "click", "name": long_link, "role": "link"}).level == "allow"


def test_kill_flag_roundtrip(tmp_path, monkeypatch):
    from deskpilot import killswitch

    monkeypatch.setattr(killswitch, "HOME", tmp_path)
    monkeypatch.setattr(killswitch, "KILL_FLAG", tmp_path / "killed")
    assert not killswitch.killed()
    killswitch.write_flag("test")
    assert killswitch.killed()
    killswitch.clear_flag()
    killswitch.clear_flag()  # idempotent
    assert not killswitch.killed()


def test_kill_never_targets_itself():
    import os

    from deskpilot.killswitch import victims

    assert all(p.pid != os.getpid() for p in victims(os.getpid(), os.getpid()))
    assert all(p.pid != os.getpid() for p in victims(999999999, os.getpid()))


def test_kill_recognises_only_deskpilot_processes():
    from deskpilot.killswitch import is_deskpilot

    exe = r"C:\Users\u\.local\bin\deskpilot.exe"
    assert is_deskpilot("deskpilot.exe", [exe, "serve"])
    assert is_deskpilot("deskpilot.exe", [exe])
    assert is_deskpilot("python.exe", [r"C:\t\python.exe", exe, "serve"])
    assert is_deskpilot("python.exe", ["python.exe", "-m", "deskpilot.overlay"])
    assert is_deskpilot("python.exe", ["python.exe", "-m", "deskpilot.confirm", "click", "x", "60"])
    assert is_deskpilot("python.exe", ["python.exe", "-m", "deskpilot"])
    assert not is_deskpilot("bash.exe", ["bash", "-c", "deskpilot.exe serve"])
    assert not is_deskpilot("python.exe", ["python.exe", "-c", "print('hi')"])
    assert not is_deskpilot("deskpilot.exe", [exe, "unlock"])
    assert not is_deskpilot("deskpilot.exe", [exe, "server2"])
    assert not is_deskpilot("python.exe", ["python.exe", "-m", "pytest", "tests/test_core.py"])


def test_format_trims_without_losing_information():
    R = (10, 10, 300, 40)
    root = Node("window", "Chat", rect=(0, 0, 1920, 1080), children=[
        # Chrome nests an identical wrapper: say it once
        Node("row", "Ann 9:41 see you", rect=R, children=[
            Node("row", "Ann 9:41 see you", rect=R, children=[
                Node("row", "Ann 9:41", rect=(10, 10, 200, 25)),          # echo of the parent: folded
                Node("button", "Reply", rect=(250, 10, 290, 40)),          # new information: kept
            ]),
        ]),
        Node("row", "", rect=(10, 50, 300, 80)),                            # unnamed, stateless: skipped
        Node("row", "", rect=(10, 90, 300, 120), selected=True),            # unnamed but selected: kept
        Node("toolbar", "Bookmarks", rect=(0, 130, 1920, 160), children=[
            Node("button", f"Site {i}", rect=(10 + 60 * i, 130, 60 + 60 * i, 160)) for i in range(12)]),
    ])
    lines, by_ref, _ = format_tree(root, AREA, RefTable())
    text = "\n".join(lines)
    assert text.count('row "Ann 9:41 see you"') == 1
    assert 'row "Ann 9:41"' not in text
    assert 'button "Reply"' in text
    assert sum(1 for line in lines if line.strip().split(" ", 1)[1] == "row selected") == 1
    assert "Site 3" not in text and "12 items folded" in text
    # a filter still reaches folded bookmarks, and find() always does
    flines, _, _ = format_tree(root, AREA, RefTable(), filt="Site 3")
    assert any("Site 3" in line for line in flines)
    assert find_nodes(root, "Site 3", None, AREA)
